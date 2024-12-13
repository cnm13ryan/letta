# inspecting tools
import json
import os
import traceback
import warnings
from abc import abstractmethod
from asyncio import Lock
from datetime import datetime
from typing import Callable, List, Optional, Tuple, Union

from composio.client import Composio
from composio.client.collections import ActionModel, AppModel
from fastapi import HTTPException

import letta.constants as constants
import letta.server.utils as server_utils
import letta.system as system
from letta.agent import Agent, save_agent
from letta.chat_only_agent import ChatOnlyAgent
from letta.credentials import LettaCredentials
from letta.data_sources.connectors import DataConnector, load_data
from letta.errors import LettaAgentNotFoundError

# TODO use custom interface
from letta.interface import AgentInterface  # abstract
from letta.interface import CLIInterface  # for printing to terminal
from letta.log import get_logger
from letta.metadata import MetadataStore
from letta.o1_agent import O1Agent
from letta.offline_memory_agent import OfflineMemoryAgent
from letta.orm import Base
from letta.orm.errors import NoResultFound
from letta.providers import (
    AnthropicProvider,
    AzureProvider,
    GoogleAIProvider,
    GroqProvider,
    LettaProvider,
    OllamaProvider,
    OpenAIProvider,
    Provider,
    TogetherProvider,
    VLLMChatCompletionsProvider,
    VLLMCompletionsProvider,
)
from letta.schemas.agent import AgentState, AgentType, CreateAgent, UpdateAgent
from letta.schemas.api_key import APIKey, APIKeyCreate
from letta.schemas.block import BlockUpdate
from letta.schemas.embedding_config import EmbeddingConfig

# openai schemas
from letta.schemas.enums import JobStatus
from letta.schemas.job import Job, JobUpdate
from letta.schemas.letta_message import FunctionReturn, LettaMessage
from letta.schemas.llm_config import LLMConfig
from letta.schemas.memory import (
    ArchivalMemorySummary,
    ContextWindowOverview,
    Memory,
    RecallMemorySummary,
)
from letta.schemas.message import Message, MessageCreate, MessageRole, MessageUpdate
from letta.schemas.organization import Organization
from letta.schemas.passage import Passage
from letta.schemas.source import Source
from letta.schemas.tool import Tool, ToolCreate
from letta.schemas.usage import LettaUsageStatistics
from letta.schemas.user import User
from letta.services.agent_manager import AgentManager
from letta.services.block_manager import BlockManager
from letta.services.job_manager import JobManager
from letta.services.message_manager import MessageManager
from letta.services.organization_manager import OrganizationManager
from letta.services.passage_manager import PassageManager
from letta.services.per_agent_lock_manager import PerAgentLockManager
from letta.services.sandbox_config_manager import SandboxConfigManager
from letta.services.source_manager import SourceManager
from letta.services.tool_execution_sandbox import ToolExecutionSandbox
from letta.services.tool_manager import ToolManager
from letta.services.user_manager import UserManager
from letta.utils import get_utc_time, json_dumps, json_loads

logger = get_logger(__name__)


class Server(object):
    """Abstract server class that supports multi-agent multi-user"""

    @abstractmethod
    def list_agents(self, user_id: str) -> dict:
        """List all available agents to a user"""
        raise NotImplementedError

    @abstractmethod
    def get_agent_memory(self, user_id: str, agent_id: str) -> dict:
        """Return the memory of an agent (core memory + non-core statistics)"""
        raise NotImplementedError

    @abstractmethod
    def get_server_config(self, user_id: str) -> dict:
        """Return the base config"""
        raise NotImplementedError

    @abstractmethod
    def update_agent_core_memory(self, user_id: str, agent_id: str, label: str, actor: User) -> Memory:
        """Update the agents core memory block, return the new state"""
        raise NotImplementedError

    @abstractmethod
    def create_agent(
        self,
        request: CreateAgent,
        actor: User,
        # interface
        interface: Union[AgentInterface, None] = None,
    ) -> AgentState:
        """Create a new agent using a config"""
        raise NotImplementedError

    @abstractmethod
    def user_message(self, user_id: str, agent_id: str, message: str) -> None:
        """Process a message from the user, internally calls step"""
        raise NotImplementedError

    @abstractmethod
    def system_message(self, user_id: str, agent_id: str, message: str) -> None:
        """Process a message from the system, internally calls step"""
        raise NotImplementedError

    @abstractmethod
    def send_messages(self, user_id: str, agent_id: str, messages: Union[MessageCreate, List[Message]]) -> None:
        """Send a list of messages to the agent"""
        raise NotImplementedError

    @abstractmethod
    def run_command(self, user_id: str, agent_id: str, command: str) -> Union[str, None]:
        """Run a command on the agent, e.g. /memory

        May return a string with a message generated by the command
        """
        raise NotImplementedError


from contextlib import contextmanager

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from letta.config import LettaConfig

# NOTE: hack to see if single session management works
from letta.settings import model_settings, settings, tool_settings

config = LettaConfig.load()


def print_sqlite_schema_error():
    """Print a formatted error message for SQLite schema issues"""
    console = Console()
    error_text = Text()
    error_text.append("Existing SQLite DB schema is invalid, and schema migrations are not supported for SQLite. ", style="bold red")
    error_text.append("To have migrations supported between Letta versions, please run Letta with Docker (", style="white")
    error_text.append("https://docs.letta.com/server/docker", style="blue underline")
    error_text.append(") or use Postgres by setting ", style="white")
    error_text.append("LETTA_PG_URI", style="yellow")
    error_text.append(".\n\n", style="white")
    error_text.append("If you wish to keep using SQLite, you can reset your database by removing the DB file with ", style="white")
    error_text.append("rm ~/.letta/sqlite.db", style="yellow")
    error_text.append(" or downgrade to your previous version of Letta.", style="white")

    console.print(Panel(error_text, border_style="red"))


@contextmanager
def db_error_handler():
    """Context manager for handling database errors"""
    try:
        yield
    except Exception as e:
        # Handle other SQLAlchemy errors
        print(e)
        print_sqlite_schema_error()
        # raise ValueError(f"SQLite DB error: {str(e)}")
        exit(1)


if settings.letta_pg_uri_no_default:
    config.recall_storage_type = "postgres"
    config.recall_storage_uri = settings.letta_pg_uri_no_default
    config.archival_storage_type = "postgres"
    config.archival_storage_uri = settings.letta_pg_uri_no_default

    # create engine
    engine = create_engine(settings.letta_pg_uri)
else:
    # TODO: don't rely on config storage
    engine = create_engine("sqlite:///" + os.path.join(config.recall_storage_path, "sqlite.db"))

    # Store the original connect method
    original_connect = engine.connect

    def wrapped_connect(*args, **kwargs):
        with db_error_handler():
            # Get the connection
            connection = original_connect(*args, **kwargs)

            # Store the original execution method
            original_execute = connection.execute

            # Wrap the execute method of the connection
            def wrapped_execute(*args, **kwargs):
                with db_error_handler():
                    return original_execute(*args, **kwargs)

            # Replace the connection's execute method
            connection.execute = wrapped_execute

            return connection

    # Replace the engine's connect method
    engine.connect = wrapped_connect

    Base.metadata.create_all(bind=engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


from contextlib import contextmanager

db_context = contextmanager(get_db)


class SyncServer(Server):
    """Simple single-threaded / blocking server process"""

    def __init__(
        self,
        chaining: bool = True,
        max_chaining_steps: Optional[bool] = None,
        default_interface_factory: Callable[[], AgentInterface] = lambda: CLIInterface(),
        init_with_default_org_and_user: bool = True,
        # default_interface: AgentInterface = CLIInterface(),
        # default_persistence_manager_cls: PersistenceManager = LocalStateManager,
        # auth_mode: str = "none",  # "none, "jwt", "external"
    ):
        """Server process holds in-memory agents that are being run"""
        # chaining = whether or not to run again if request_heartbeat=true
        self.chaining = chaining

        # if chaining == true, what's the max number of times we'll chain before yielding?
        # none = no limit, can go on forever
        self.max_chaining_steps = max_chaining_steps

        # The default interface that will get assigned to agents ON LOAD
        self.default_interface_factory = default_interface_factory

        self.credentials = LettaCredentials.load()

        # Locks
        self.send_message_lock = Lock()

        # Initialize the metadata store
        config = LettaConfig.load()
        if settings.letta_pg_uri_no_default:
            config.recall_storage_type = "postgres"
            config.recall_storage_uri = settings.letta_pg_uri_no_default
            config.archival_storage_type = "postgres"
            config.archival_storage_uri = settings.letta_pg_uri_no_default
        config.save()
        self.config = config
        self.ms = MetadataStore(self.config)

        # Managers that interface with data models
        self.organization_manager = OrganizationManager()
        self.passage_manager = PassageManager()
        self.user_manager = UserManager()
        self.tool_manager = ToolManager()
        self.block_manager = BlockManager()
        self.source_manager = SourceManager()
        self.sandbox_config_manager = SandboxConfigManager(tool_settings)
        self.message_manager = MessageManager()
        self.job_manager = JobManager()
        self.agent_manager = AgentManager()

        # Managers that interface with parallelism
        self.per_agent_lock_manager = PerAgentLockManager()

        # Make default user and org
        if init_with_default_org_and_user:
            self.default_org = self.organization_manager.create_default_organization()
            self.default_user = self.user_manager.create_default_user()
            self.block_manager.add_default_blocks(actor=self.default_user)
            self.tool_manager.add_base_tools(actor=self.default_user)

            # If there is a default org/user
            # This logic may have to change in the future
            if settings.load_default_external_tools:
                self.add_default_external_tools(actor=self.default_user)

        # collect providers (always has Letta as a default)
        self._enabled_providers: List[Provider] = [LettaProvider()]
        if model_settings.openai_api_key:
            self._enabled_providers.append(
                OpenAIProvider(
                    api_key=model_settings.openai_api_key,
                    base_url=model_settings.openai_api_base,
                )
            )
        if model_settings.anthropic_api_key:
            self._enabled_providers.append(
                AnthropicProvider(
                    api_key=model_settings.anthropic_api_key,
                )
            )
        if model_settings.ollama_base_url:
            self._enabled_providers.append(
                OllamaProvider(
                    base_url=model_settings.ollama_base_url,
                    api_key=None,
                    default_prompt_formatter=model_settings.default_prompt_formatter,
                )
            )
        if model_settings.gemini_api_key:
            self._enabled_providers.append(
                GoogleAIProvider(
                    api_key=model_settings.gemini_api_key,
                )
            )
        if model_settings.azure_api_key and model_settings.azure_base_url:
            assert model_settings.azure_api_version, "AZURE_API_VERSION is required"
            self._enabled_providers.append(
                AzureProvider(
                    api_key=model_settings.azure_api_key,
                    base_url=model_settings.azure_base_url,
                    api_version=model_settings.azure_api_version,
                )
            )
        if model_settings.groq_api_key:
            self._enabled_providers.append(
                GroqProvider(
                    api_key=model_settings.groq_api_key,
                )
            )
        if model_settings.together_api_key:
            self._enabled_providers.append(
                TogetherProvider(
                    api_key=model_settings.together_api_key,
                    default_prompt_formatter=model_settings.default_prompt_formatter,
                )
            )
        if model_settings.vllm_api_base:
            # vLLM exposes both a /chat/completions and a /completions endpoint
            self._enabled_providers.append(
                VLLMCompletionsProvider(
                    base_url=model_settings.vllm_api_base,
                    default_prompt_formatter=model_settings.default_prompt_formatter,
                )
            )
            # NOTE: to use the /chat/completions endpoint, you need to specify extra flags on vLLM startup
            # see: https://docs.vllm.ai/en/latest/getting_started/examples/openai_chat_completion_client_with_tools.html
            # e.g. "... --enable-auto-tool-choice --tool-call-parser hermes"
            self._enabled_providers.append(
                VLLMChatCompletionsProvider(
                    base_url=model_settings.vllm_api_base,
                )
            )

    def initialize_agent(self, agent_id, actor, interface: Union[AgentInterface, None] = None, initial_message_sequence=None) -> Agent:
        """Initialize an agent from the database"""
        agent_state = self.agent_manager.get_agent_by_id(agent_id=agent_id, actor=actor)

        interface = interface or self.default_interface_factory()
        if agent_state.agent_type == AgentType.memgpt_agent:
            agent = Agent(agent_state=agent_state, interface=interface, user=actor, initial_message_sequence=initial_message_sequence)
        elif agent_state.agent_type == AgentType.offline_memory_agent:
            agent = OfflineMemoryAgent(
                agent_state=agent_state, interface=interface, user=actor, initial_message_sequence=initial_message_sequence
            )
        else:
            assert initial_message_sequence is None, f"Initial message sequence is not supported for O1Agents"
            agent = O1Agent(agent_state=agent_state, interface=interface, user=actor)

        # Persist to agent
        save_agent(agent)
        return agent

    def load_agent(self, agent_id: str, actor: User, interface: Union[AgentInterface, None] = None) -> Agent:
        """Updated method to load agents from persisted storage"""
        agent_lock = self.per_agent_lock_manager.get_lock(agent_id)
        with agent_lock:
            agent_state = self.agent_manager.get_agent_by_id(agent_id=agent_id, actor=actor)

            if agent_state is None:
                raise LettaAgentNotFoundError(f"Agent (agent_id={agent_id}) does not exist")
            elif agent_state.created_by_id is None:
                raise ValueError(f"Agent (agent_id={agent_id}) does not have a user_id")
            actor = self.user_manager.get_user_by_id(user_id=agent_state.created_by_id)

            interface = interface or self.default_interface_factory()
            if agent_state.agent_type == AgentType.memgpt_agent:
                agent = Agent(agent_state=agent_state, interface=interface, user=actor)
            elif agent_state.agent_type == AgentType.o1_agent:
                agent = O1Agent(agent_state=agent_state, interface=interface, user=actor)
            elif agent_state.agent_type == AgentType.offline_memory_agent:
                agent = OfflineMemoryAgent(agent_state=agent_state, interface=interface, user=actor)
            elif agent_state.agent_type == AgentType.chat_only_agent:
                agent = ChatOnlyAgent(agent_state=agent_state, interface=interface, user=actor)
            else:
                raise ValueError(f"Invalid agent type {agent_state.agent_type}")

            # Rebuild the system prompt - may be linked to new blocks now
            agent.rebuild_system_prompt()

            # Persist to agent
            save_agent(agent)
            return agent

    def _step(
        self,
        actor: User,
        agent_id: str,
        input_messages: Union[Message, List[Message]],
        interface: Union[AgentInterface, None] = None,  # needed to getting responses
        # timestamp: Optional[datetime],
    ) -> LettaUsageStatistics:
        """Send the input message through the agent"""
        # TODO: Thread actor directly through this function, since the top level caller most likely already retrieved the user
        # Input validation
        if isinstance(input_messages, Message):
            input_messages = [input_messages]
        if not all(isinstance(m, Message) for m in input_messages):
            raise ValueError(f"messages should be a Message or a list of Message, got {type(input_messages)}")

        logger.debug(f"Got input messages: {input_messages}")
        letta_agent = None
        try:
            letta_agent = self.load_agent(agent_id=agent_id, interface=interface, actor=actor)
            if letta_agent is None:
                raise KeyError(f"Agent (user={user_id}, agent={agent_id}) is not loaded")

            # Determine whether or not to token stream based on the capability of the interface
            token_streaming = letta_agent.interface.streaming_mode if hasattr(letta_agent.interface, "streaming_mode") else False

            logger.debug(f"Starting agent step")
            usage_stats = letta_agent.step(
                messages=input_messages,
                chaining=self.chaining,
                max_chaining_steps=self.max_chaining_steps,
                stream=token_streaming,
                ms=self.ms,
                skip_verify=True,
            )

            # save agent after step
            save_agent(letta_agent)

        except Exception as e:
            logger.error(f"Error in server._step: {e}")
            print(traceback.print_exc())
            raise
        finally:
            logger.debug("Calling step_yield()")
            if letta_agent:
                letta_agent.interface.step_yield()

        return usage_stats

    def _command(self, user_id: str, agent_id: str, command: str) -> LettaUsageStatistics:
        """Process a CLI command"""
        # TODO: Thread actor directly through this function, since the top level caller most likely already retrieved the user
        actor = self.user_manager.get_user_or_default(user_id=user_id)

        logger.debug(f"Got command: {command}")

        # Get the agent object (loaded in memory)
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)
        usage = None

        if command.lower() == "exit":
            # exit not supported on server.py
            raise ValueError(command)

        elif command.lower() == "save" or command.lower() == "savechat":
            save_agent(letta_agent)

        elif command.lower() == "attach":
            # Different from CLI, we extract the data source name from the command
            command = command.strip().split()
            try:
                data_source = int(command[1])
            except:
                raise ValueError(command)

            # attach data to agent from source
            letta_agent.attach_source(
                user=self.user_manager.get_user_by_id(user_id=user_id),
                source_id=data_source,
                source_manager=self.source_manager,
                agent_manager=self.agent_manager,
            )

        elif command.lower() == "dump" or command.lower().startswith("dump "):
            # Check if there's an additional argument that's an integer
            command = command.strip().split()
            amount = int(command[1]) if len(command) > 1 and command[1].isdigit() else 0
            if amount == 0:
                letta_agent.interface.print_messages(letta_agent.messages, dump=True)
            else:
                letta_agent.interface.print_messages(letta_agent.messages[-min(amount, len(letta_agent.messages)) :], dump=True)

        elif command.lower() == "dumpraw":
            letta_agent.interface.print_messages_raw(letta_agent.messages)

        elif command.lower() == "memory":
            ret_str = f"\nDumping memory contents:\n" + f"\n{str(letta_agent.agent_state.memory)}" + f"\n{str(letta_agent.passage_manager)}"
            return ret_str

        elif command.lower() == "pop" or command.lower().startswith("pop "):
            # Check if there's an additional argument that's an integer
            command = command.strip().split()
            pop_amount = int(command[1]) if len(command) > 1 and command[1].isdigit() else 3
            n_messages = len(letta_agent.messages)
            MIN_MESSAGES = 2
            if n_messages <= MIN_MESSAGES:
                logger.debug(f"Agent only has {n_messages} messages in stack, none left to pop")
            elif n_messages - pop_amount < MIN_MESSAGES:
                logger.debug(f"Agent only has {n_messages} messages in stack, cannot pop more than {n_messages - MIN_MESSAGES}")
            else:
                logger.debug(f"Popping last {pop_amount} messages from stack")
                for _ in range(min(pop_amount, len(letta_agent.messages))):
                    letta_agent.messages.pop()

        elif command.lower() == "retry":
            # TODO this needs to also modify the persistence manager
            logger.debug(f"Retrying for another answer")
            while len(letta_agent.messages) > 0:
                if letta_agent.messages[-1].get("role") == "user":
                    # we want to pop up to the last user message and send it again
                    letta_agent.messages[-1].get("content")
                    letta_agent.messages.pop()
                    break
                letta_agent.messages.pop()

        elif command.lower() == "rethink" or command.lower().startswith("rethink "):
            # TODO this needs to also modify the persistence manager
            if len(command) < len("rethink "):
                logger.warning("Missing text after the command")
            else:
                for x in range(len(letta_agent.messages) - 1, 0, -1):
                    if letta_agent.messages[x].get("role") == "assistant":
                        text = command[len("rethink ") :].strip()
                        letta_agent.messages[x].update({"content": text})
                        break

        elif command.lower() == "rewrite" or command.lower().startswith("rewrite "):
            # TODO this needs to also modify the persistence manager
            if len(command) < len("rewrite "):
                logger.warning("Missing text after the command")
            else:
                for x in range(len(letta_agent.messages) - 1, 0, -1):
                    if letta_agent.messages[x].get("role") == "assistant":
                        text = command[len("rewrite ") :].strip()
                        args = json_loads(letta_agent.messages[x].get("function_call").get("arguments"))
                        args["message"] = text
                        letta_agent.messages[x].get("function_call").update({"arguments": json_dumps(args)})
                        break

        # No skip options
        elif command.lower() == "wipe":
            # exit not supported on server.py
            raise ValueError(command)

        elif command.lower() == "heartbeat":
            input_message = system.get_heartbeat()
            usage = self._step(actor=actor, agent_id=agent_id, input_message=input_message)

        elif command.lower() == "memorywarning":
            input_message = system.get_token_limit_warning()
            usage = self._step(actor=actor, agent_id=agent_id, input_message=input_message)

        if not usage:
            usage = LettaUsageStatistics()

        return usage

    def user_message(
        self,
        user_id: str,
        agent_id: str,
        message: Union[str, Message],
        timestamp: Optional[datetime] = None,
    ) -> LettaUsageStatistics:
        """Process an incoming user message and feed it through the Letta agent"""
        try:
            actor = self.user_manager.get_user_by_id(user_id=user_id)
        except NoResultFound:
            raise ValueError(f"User user_id={user_id} does not exist")

        try:
            agent = self.agent_manager.get_agent_by_id(agent_id=agent_id, actor=actor)
        except NoResultFound:
            raise ValueError(f"Agent agent_id={agent_id} does not exist")

        # Basic input sanitization
        if isinstance(message, str):
            if len(message) == 0:
                raise ValueError(f"Invalid input: '{message}'")

            # If the input begins with a command prefix, reject
            elif message.startswith("/"):
                raise ValueError(f"Invalid input: '{message}'")

            packaged_user_message = system.package_user_message(
                user_message=message,
                time=timestamp.isoformat() if timestamp else None,
            )

            # NOTE: eventually deprecate and only allow passing Message types
            # Convert to a Message object
            if timestamp:
                message = Message(
                    agent_id=agent_id,
                    role="user",
                    text=packaged_user_message,
                    created_at=timestamp,
                )
            else:
                message = Message(
                    agent_id=agent_id,
                    role="user",
                    text=packaged_user_message,
                )

        # Run the agent state forward
        usage = self._step(actor=actor, agent_id=agent_id, input_messages=message)
        return usage

    def system_message(
        self,
        user_id: str,
        agent_id: str,
        message: Union[str, Message],
        timestamp: Optional[datetime] = None,
    ) -> LettaUsageStatistics:
        """Process an incoming system message and feed it through the Letta agent"""
        try:
            actor = self.user_manager.get_user_by_id(user_id=user_id)
        except NoResultFound:
            raise ValueError(f"User user_id={user_id} does not exist")

        try:
            agent = self.agent_manager.get_agent_by_id(agent_id=agent_id, actor=actor)
        except NoResultFound:
            raise ValueError(f"Agent agent_id={agent_id} does not exist")

        # Basic input sanitization
        if isinstance(message, str):
            if len(message) == 0:
                raise ValueError(f"Invalid input: '{message}'")

            # If the input begins with a command prefix, reject
            elif message.startswith("/"):
                raise ValueError(f"Invalid input: '{message}'")

            packaged_system_message = system.package_system_message(system_message=message)

            # NOTE: eventually deprecate and only allow passing Message types
            # Convert to a Message object

            if timestamp:
                message = Message(
                    agent_id=agent_id,
                    role="system",
                    text=packaged_system_message,
                    created_at=timestamp,
                )
            else:
                message = Message(
                    agent_id=agent_id,
                    role="system",
                    text=packaged_system_message,
                )

        if isinstance(message, Message):
            # Can't have a null text field
            if message.text is None or len(message.text) == 0:
                raise ValueError(f"Invalid input: '{message.text}'")
            # If the input begins with a command prefix, reject
            elif message.text.startswith("/"):
                raise ValueError(f"Invalid input: '{message.text}'")

        else:
            raise TypeError(f"Invalid input: '{message}' - type {type(message)}")

        if timestamp:
            # Override the timestamp with what the caller provided
            message.created_at = timestamp

        # Run the agent state forward
        return self._step(actor=actor, agent_id=agent_id, input_messages=message)

    def send_messages(
        self,
        actor: User,
        agent_id: str,
        messages: Union[List[MessageCreate], List[Message]],
        # whether or not to wrap user and system message as MemGPT-style stringified JSON
        wrap_user_message: bool = True,
        wrap_system_message: bool = True,
        interface: Union[AgentInterface, None] = None,  # needed to getting responses
    ) -> LettaUsageStatistics:
        """Send a list of messages to the agent

        If the messages are of type MessageCreate, we need to turn them into
        Message objects first before sending them through step.

        Otherwise, we can pass them in directly.
        """
        message_objects: List[Message] = []

        if all(isinstance(m, MessageCreate) for m in messages):
            for message in messages:
                assert isinstance(message, MessageCreate)

                # If wrapping is eanbled, wrap with metadata before placing content inside the Message object
                if message.role == MessageRole.user and wrap_user_message:
                    message.text = system.package_user_message(user_message=message.text)
                elif message.role == MessageRole.system and wrap_system_message:
                    message.text = system.package_system_message(system_message=message.text)
                else:
                    raise ValueError(f"Invalid message role: {message.role}")

                # Create the Message object
                message_objects.append(
                    Message(
                        agent_id=agent_id,
                        role=message.role,
                        text=message.text,
                        name=message.name,
                        # assigned later?
                        model=None,
                        # irrelevant
                        tool_calls=None,
                        tool_call_id=None,
                    )
                )

        elif all(isinstance(m, Message) for m in messages):
            for message in messages:
                assert isinstance(message, Message)
                message_objects.append(message)

        else:
            raise ValueError(f"All messages must be of type Message or MessageCreate, got {[type(message) for message in messages]}")

        # Run the agent state forward
        return self._step(actor=actor, agent_id=agent_id, input_messages=message_objects, interface=interface)

    # @LockingServer.agent_lock_decorator
    def run_command(self, user_id: str, agent_id: str, command: str) -> LettaUsageStatistics:
        """Run a command on the agent"""
        # If the input begins with a command prefix, attempt to process it as a command
        if command.startswith("/"):
            if len(command) > 1:
                command = command[1:]  # strip the prefix
        return self._command(user_id=user_id, agent_id=agent_id, command=command)

    def create_agent(
        self,
        request: CreateAgent,
        actor: User,
        # interface
        interface: Union[AgentInterface, None] = None,
    ) -> AgentState:
        """Create a new agent using a config"""
        # Invoke manager
        agent_state = self.agent_manager.create_agent(
            agent_create=request,
            actor=actor,
        )

        # create the agent object
        if request.initial_message_sequence is not None:
            # init_messages = [Message(user_id=user_id, agent_id=agent_state.id, role=message.role, text=message.text) for message in request.initial_message_sequence]
            init_messages = []
            for message in request.initial_message_sequence:

                if message.role == MessageRole.user:
                    packed_message = system.package_user_message(
                        user_message=message.text,
                    )
                elif message.role == MessageRole.system:
                    packed_message = system.package_system_message(
                        system_message=message.text,
                    )
                else:
                    raise ValueError(f"Invalid message role: {message.role}")

                init_messages.append(Message(role=message.role, text=packed_message, agent_id=agent_state.id))
            # init_messages = [Message.dict_to_message(user_id=user_id, agent_id=agent_state.id, openai_message_dict=message.model_dump()) for message in request.initial_message_sequence]
        else:
            init_messages = None

        # initialize the agent (generates initial message list with system prompt)
        if interface is None:
            interface = self.default_interface_factory()
        self.initialize_agent(agent_id=agent_state.id, interface=interface, initial_message_sequence=init_messages, actor=actor)

        in_memory_agent_state = self.agent_manager.get_agent_by_id(agent_state.id, actor=actor)
        return in_memory_agent_state

    # TODO: This is not good!
    # TODO: Ideally, this should ALL be handled by the ORM
    # TODO: The main blocker here IS the _message updates
    def update_agent(
        self,
        agent_id: str,
        request: UpdateAgent,
        actor: User,
    ) -> AgentState:
        """Update the agents core memory block, return the new state"""
        # Get the agent object (loaded in memory)
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)

        # Update tags
        if request.tags is not None:  # Allow for empty list
            letta_agent.agent_state.tags = request.tags

        # update the system prompt
        if request.system:
            letta_agent.update_system_prompt(request.system)

        # update in-context messages
        if request.message_ids:
            # This means the user is trying to change what messages are in the message buffer
            # Internally this requires (1) pulling from recall,
            # then (2) setting the attributes ._messages and .state.message_ids
            letta_agent.set_message_buffer(message_ids=request.message_ids)

        # tools
        if request.tool_ids:
            # Replace tools and also re-link

            # (1) get tools + make sure they exist
            # Current and target tools as sets of tool names
            current_tools = letta_agent.agent_state.tools
            current_tool_ids = set([t.id for t in current_tools])
            target_tool_ids = set(request.tool_ids)

            # Calculate tools to add and remove
            tool_ids_to_add = target_tool_ids - current_tool_ids
            tools_ids_to_remove = current_tool_ids - target_tool_ids

            # update agent tool list
            for tool_id in tools_ids_to_remove:
                self.remove_tool_from_agent(agent_id=agent_id, tool_id=tool_id, user_id=actor.id)
            for tool_id in tool_ids_to_add:
                self.add_tool_to_agent(agent_id=agent_id, tool_id=tool_id, user_id=actor.id)

            # reload agent
            letta_agent = self.load_agent(agent_id=agent_id, actor=actor)

        # configs
        if request.llm_config:
            letta_agent.agent_state.llm_config = request.llm_config
        if request.embedding_config:
            letta_agent.agent_state.embedding_config = request.embedding_config

        # other minor updates
        if request.name:
            letta_agent.agent_state.name = request.name
        if request.metadata_:
            letta_agent.agent_state.metadata_ = request.metadata_

        # save the agent
        save_agent(letta_agent)
        # TODO: probably reload the agent somehow?
        return letta_agent.agent_state

    def get_tools_from_agent(self, agent_id: str, user_id: Optional[str]) -> List[Tool]:
        """Get tools from an existing agent"""
        # TODO: Thread actor directly through this function, since the top level caller most likely already retrieved the user
        actor = self.user_manager.get_user_or_default(user_id=user_id)

        # Get the agent object (loaded in memory)
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)
        return letta_agent.agent_state.tools

    def add_tool_to_agent(
        self,
        agent_id: str,
        tool_id: str,
        user_id: str,
    ):
        """Add tools from an existing agent"""
        # TODO: Thread actor directly through this function, since the top level caller most likely already retrieved the user
        actor = self.user_manager.get_user_or_default(user_id=user_id)

        # Get the agent object (loaded in memory)
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)

        # Get all the tool objects from the request
        tool_objs = []
        tool_obj = self.tool_manager.get_tool_by_id(tool_id=tool_id, actor=actor)
        assert tool_obj, f"Tool with id={tool_id} does not exist"
        tool_objs.append(tool_obj)

        for tool in letta_agent.agent_state.tools:
            tool_obj = self.tool_manager.get_tool_by_id(tool_id=tool.id, actor=actor)
            assert tool_obj, f"Tool with id={tool.id} does not exist"

            # If it's not the already added tool
            if tool_obj.id != tool_id:
                tool_objs.append(tool_obj)

        # replace the list of tool names ("ids") inside the agent state
        letta_agent.agent_state.tools = tool_objs

        # then attempt to link the tools modules
        letta_agent.link_tools(tool_objs)

        # save the agent
        save_agent(letta_agent)
        return letta_agent.agent_state

    def remove_tool_from_agent(
        self,
        agent_id: str,
        tool_id: str,
        user_id: str,
    ):
        """Remove tools from an existing agent"""
        # TODO: Thread actor directly through this function, since the top level caller most likely already retrieved the user
        actor = self.user_manager.get_user_or_default(user_id=user_id)

        # Get the agent object (loaded in memory)
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)

        # Get all the tool_objs
        tool_objs = []
        for tool in letta_agent.agent_state.tools:
            tool_obj = self.tool_manager.get_tool_by_id(tool_id=tool.id, actor=actor)
            assert tool_obj, f"Tool with id={tool.id} does not exist"

            # If it's not the tool we want to remove
            if tool_obj.id != tool_id:
                tool_objs.append(tool_obj)

        # replace the list of tool names ("ids") inside the agent state
        letta_agent.agent_state.tools = tool_objs

        # then attempt to link the tools modules
        letta_agent.link_tools(tool_objs)

        # save the agent
        save_agent(letta_agent)
        return letta_agent.agent_state

    # convert name->id

    def get_agent_memory(self, agent_id: str, actor: User) -> Memory:
        """Return the memory of an agent (core memory)"""
        agent = self.load_agent(agent_id=agent_id, actor=actor)
        return agent.agent_state.memory

    def get_archival_memory_summary(self, agent_id: str, actor: User) -> ArchivalMemorySummary:
        agent = self.load_agent(agent_id=agent_id, actor=actor)
        return ArchivalMemorySummary(size=agent.passage_manager.size(actor=self.default_user))

    def get_recall_memory_summary(self, agent_id: str, actor: User) -> RecallMemorySummary:
        agent = self.load_agent(agent_id=agent_id, actor=actor)
        return RecallMemorySummary(size=len(agent.message_manager))

    def get_in_context_messages(self, agent_id: str, actor: User) -> List[Message]:
        """Get the in-context messages in the agent's memory"""
        # Get the agent object (loaded in memory)
        agent = self.load_agent(agent_id=agent_id, actor=actor)
        return agent._messages

    def get_agent_archival(self, user_id: str, agent_id: str, cursor: Optional[str] = None, limit: int = 50) -> List[Passage]:
        """Paginated query of all messages in agent archival memory"""
        # TODO: Thread actor directly through this function, since the top level caller most likely already retrieved the user
        actor = self.user_manager.get_user_or_default(user_id=user_id)

        # Get the agent object (loaded in memory)
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)

        # iterate over records
        records = letta_agent.passage_manager.list_passages(
            actor=actor,
            agent_id=agent_id,
            cursor=cursor,
            limit=limit,
        )

        return records

    def get_agent_archival_cursor(
        self,
        user_id: str,
        agent_id: str,
        cursor: Optional[str] = None,
        limit: Optional[int] = 100,
        order_by: Optional[str] = "created_at",
        reverse: Optional[bool] = False,
    ) -> List[Passage]:
        # TODO: Thread actor directly through this function, since the top level caller most likely already retrieved the user
        actor = self.user_manager.get_user_or_default(user_id=user_id)

        # Get the agent object (loaded in memory)
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)

        # iterate over records
        records = letta_agent.passage_manager.list_passages(
            actor=self.default_user,
            agent_id=agent_id,
            cursor=cursor,
            limit=limit,
        )
        return records

    def insert_archival_memory(self, agent_id: str, memory_contents: str, actor: User) -> List[Passage]:
        # Get the agent object (loaded in memory)
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)

        # Insert into archival memory
        passages = self.passage_manager.insert_passage(
            agent_state=letta_agent.agent_state, agent_id=agent_id, text=memory_contents, actor=actor
        )

        save_agent(letta_agent)

        return passages

    def delete_archival_memory(self, agent_id: str, memory_id: str, actor: User):
        # Get the agent object (loaded in memory)
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)

        # Delete by ID
        # TODO check if it exists first, and throw error if not
        letta_agent.passage_manager.delete_passage_by_id(passage_id=memory_id, actor=actor)

        # TODO: return archival memory

    def get_agent_recall_cursor(
        self,
        user_id: str,
        agent_id: str,
        after: Optional[str] = None,
        before: Optional[str] = None,
        limit: Optional[int] = 100,
        reverse: Optional[bool] = False,
        return_message_object: bool = True,
        assistant_message_tool_name: str = constants.DEFAULT_MESSAGE_TOOL,
        assistant_message_tool_kwarg: str = constants.DEFAULT_MESSAGE_TOOL_KWARG,
    ) -> Union[List[Message], List[LettaMessage]]:
        # TODO: Thread actor directly through this function, since the top level caller most likely already retrieved the user
        actor = self.user_manager.get_user_or_default(user_id=user_id)

        # Get the agent object (loaded in memory)
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)

        # iterate over records
        start_date = self.message_manager.get_message_by_id(after, actor=actor).created_at if after else None
        end_date = self.message_manager.get_message_by_id(before, actor=actor).created_at if before else None
        records = letta_agent.message_manager.list_messages_for_agent(
            agent_id=agent_id,
            actor=actor,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            ascending=not reverse,
        )

        assert all(isinstance(m, Message) for m in records)

        if not return_message_object:
            # If we're GETing messages in reverse, we need to reverse the inner list (generated by to_letta_message)
            records = [
                msg
                for m in records
                for msg in m.to_letta_message(
                    assistant_message_tool_name=assistant_message_tool_name,
                    assistant_message_tool_kwarg=assistant_message_tool_kwarg,
                )
            ]

        if reverse:
            records = records[::-1]

        return records

    def get_server_config(self, include_defaults: bool = False) -> dict:
        """Return the base config"""

        def clean_keys(config):
            config_copy = config.copy()
            for k, v in config.items():
                if k == "key" or "_key" in k:
                    config_copy[k] = server_utils.shorten_key_middle(v, chars_each_side=5)
            return config_copy

        # TODO: do we need a seperate server config?
        base_config = vars(self.config)
        clean_base_config = clean_keys(base_config)

        response = {"config": clean_base_config}

        if include_defaults:
            default_config = vars(LettaConfig())
            clean_default_config = clean_keys(default_config)
            response["defaults"] = clean_default_config

        return response

    def update_agent_core_memory(self, agent_id: str, label: str, value: str, actor: User) -> Memory:
        """Update the value of a block in the agent's memory"""

        # get the block id
        block = self.agent_manager.get_block_with_label(agent_id=agent_id, block_label=label, actor=actor)

        # update the block
        self.block_manager.update_block(block_id=block.id, block_update=BlockUpdate(value=value), actor=actor)

        # load agent
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)
        return letta_agent.agent_state.memory

    def api_key_to_user(self, api_key: str) -> str:
        """Decode an API key to a user"""
        token = self.ms.get_api_key(api_key=api_key)
        user = self.user_manager.get_user_by_id(token.user_id)
        if user is None:
            raise HTTPException(status_code=403, detail="Invalid credentials")
        else:
            return user.id

    def create_api_key(self, request: APIKeyCreate) -> APIKey:  # TODO: add other fields
        """Create a new API key for a user"""
        if request.name is None:
            request.name = f"API Key {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        token = self.ms.create_api_key(user_id=request.user_id, name=request.name)
        return token

    def list_api_keys(self, user_id: str) -> List[APIKey]:
        """List all API keys for a user"""
        return self.ms.get_all_api_keys_for_user(user_id=user_id)

    def delete_api_key(self, api_key: str) -> APIKey:
        api_key_obj = self.ms.get_api_key(api_key=api_key)
        if api_key_obj is None:
            raise ValueError("API key does not exist")
        self.ms.delete_api_key(api_key=api_key)
        return api_key_obj

    def delete_source(self, source_id: str, actor: User):
        """Delete a data source"""
        self.source_manager.delete_source(source_id=source_id, actor=actor)

        # delete data from passage store
        self.passage_manager.delete_passages(actor=actor, limit=None, source_id=source_id)

        # TODO: delete data from agent passage stores (?)

    def load_file_to_source(self, source_id: str, file_path: str, job_id: str, actor: User) -> Job:

        # update job
        job = self.job_manager.get_job_by_id(job_id, actor=actor)
        job.status = JobStatus.running
        self.job_manager.update_job_by_id(job_id=job_id, job_update=JobUpdate(**job.model_dump()), actor=actor)

        # try:
        from letta.data_sources.connectors import DirectoryConnector

        source = self.source_manager.get_source_by_id(source_id=source_id)
        if source is None:
            raise ValueError(f"Source {source_id} does not exist")
        connector = DirectoryConnector(input_files=[file_path])
        num_passages, num_documents = self.load_data(user_id=source.created_by_id, source_name=source.name, connector=connector)

        # update job status
        job.status = JobStatus.completed
        job.metadata_["num_passages"] = num_passages
        job.metadata_["num_documents"] = num_documents
        self.job_manager.update_job_by_id(job_id=job_id, job_update=JobUpdate(**job.model_dump()), actor=actor)

        # update all agents who have this source attached
        agent_states = self.source_manager.list_attached_agents(source_id=source_id, actor=actor)
        for agent_state in agent_states:
            agent_id = agent_state.id
            agent = self.load_agent(agent_id=agent_id, actor=actor)
            curr_passage_size = self.passage_manager.size(actor=actor, agent_id=agent_id, source_id=source_id)
            agent.attach_source(user=actor, source_id=source_id, source_manager=self.source_manager, agent_manager=self.agent_manager)
            new_passage_size = self.passage_manager.size(actor=actor, agent_id=agent_id, source_id=source_id)
            assert new_passage_size >= curr_passage_size  # in case empty files are added

        return job

    def load_data(
        self,
        user_id: str,
        connector: DataConnector,
        source_name: str,
    ) -> Tuple[int, int]:
        """Load data from a DataConnector into a source for a specified user_id"""
        # TODO: this should be implemented as a batch job or at least async, since it may take a long time

        # load data from a data source into the document store
        user = self.user_manager.get_user_by_id(user_id=user_id)
        source = self.source_manager.get_source_by_name(source_name=source_name, actor=user)
        if source is None:
            raise ValueError(f"Data source {source_name} does not exist for user {user_id}")

        # load data into the document store
        passage_count, document_count = load_data(connector, source, self.passage_manager, self.source_manager, actor=user)
        return passage_count, document_count

    def attach_source_to_agent(
        self,
        user_id: str,
        agent_id: str,
        source_id: Optional[str] = None,
        source_name: Optional[str] = None,
    ) -> Source:
        # attach a data source to an agent
        # TODO: Thread actor directly through this function, since the top level caller most likely already retrieved the user
        actor = self.user_manager.get_user_or_default(user_id=user_id)
        if source_id:
            data_source = self.source_manager.get_source_by_id(source_id=source_id, actor=actor)
        elif source_name:
            data_source = self.source_manager.get_source_by_name(source_name=source_name, actor=actor)
        else:
            raise ValueError(f"Need to provide at least source_id or source_name to find the source.")

        assert data_source, f"Data source with id={source_id} or name={source_name} does not exist"

        # load agent
        agent = self.load_agent(agent_id=agent_id, actor=actor)

        # attach source to agent
        agent.attach_source(user=actor, source_id=data_source.id, source_manager=self.source_manager, agent_manager=self.agent_manager)

        return data_source

    def detach_source_from_agent(
        self,
        user_id: str,
        agent_id: str,
        source_id: Optional[str] = None,
        source_name: Optional[str] = None,
    ) -> Source:
        # TODO: Thread actor directly through this function, since the top level caller most likely already retrieved the user
        actor = self.user_manager.get_user_or_default(user_id=user_id)
        if source_id:
            source = self.source_manager.get_source_by_id(source_id=source_id, actor=actor)
        elif source_name:
            source = self.source_manager.get_source_by_name(source_name=source_name, actor=actor)
        else:
            raise ValueError(f"Need to provide at least source_id or source_name to find the source.")
        source_id = source.id

        # TODO: This should be done with the ORM?
        # delete all Passage objects with source_id==source_id from agent's archival memory
        agent = self.load_agent(agent_id=agent_id, actor=actor)
        agent.passage_manager.delete_passages(actor=actor, limit=100, source_id=source_id)

        # delete agent-source mapping
        self.agent_manager.detach_source(agent_id=agent_id, source_id=source_id, actor=actor)

        # return back source data
        return source

    def list_data_source_passages(self, user_id: str, source_id: str) -> List[Passage]:
        warnings.warn("list_data_source_passages is not yet implemented, returning empty list.", category=UserWarning)
        return []

    def list_all_sources(self, actor: User) -> List[Source]:
        """List all sources (w/ extra metadata) belonging to a user"""

        sources = self.source_manager.list_sources(actor=actor)

        # Add extra metadata to the sources
        sources_with_metadata = []
        for source in sources:

            # count number of passages
            num_passages = self.passage_manager.size(actor=actor, source_id=source.id)

            # TODO: add when files table implemented
            ## count number of files
            # document_conn = StorageConnector.get_storage_connector(TableType.FILES, self.config, user_id=user_id)
            # num_documents = document_conn.size({"data_source": source.name})
            num_documents = 0

            agents = self.source_manager.list_attached_agents(source_id=source.id, actor=actor)
            # add the agent name information
            attached_agents = [{"id": agent.id, "name": agent.name} for agent in agents]

            # Overwrite metadata field, should be empty anyways
            source.metadata_ = dict(
                num_documents=num_documents,
                num_passages=num_passages,
                attached_agents=attached_agents,
            )

            sources_with_metadata.append(source)

        return sources_with_metadata

    def add_default_external_tools(self, actor: User) -> bool:
        """Add default langchain tools. Return true if successful, false otherwise."""
        success = True
        tool_creates = ToolCreate.load_default_langchain_tools()
        if tool_settings.composio_api_key:
            tool_creates += ToolCreate.load_default_composio_tools()
        for tool_create in tool_creates:
            try:
                self.tool_manager.create_or_update_tool(Tool(**tool_create.model_dump()), actor=actor)
            except Exception as e:
                warnings.warn(f"An error occurred while creating tool {tool_create}: {e}")
                warnings.warn(traceback.format_exc())
                success = False

        return success

    def update_agent_message(self, agent_id: str, message_id: str, request: MessageUpdate, actor: User) -> Message:
        """Update the details of a message associated with an agent"""

        # Get the current message
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)
        response = letta_agent.update_message(message_id=message_id, request=request)
        save_agent(letta_agent)
        return response

    def rewrite_agent_message(self, agent_id: str, new_text: str, actor: User) -> Message:

        # Get the current message
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)
        response = letta_agent.rewrite_message(new_text=new_text)
        save_agent(letta_agent)
        return response

    def rethink_agent_message(self, agent_id: str, new_thought: str, actor: User) -> Message:
        # Get the current message
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)
        response = letta_agent.rethink_message(new_thought=new_thought)
        save_agent(letta_agent)
        return response

    def retry_agent_message(self, agent_id: str, actor: User) -> List[Message]:
        # Get the current message
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)
        response = letta_agent.retry_message()
        save_agent(letta_agent)
        return response

    def get_organization_or_default(self, org_id: Optional[str]) -> Organization:
        """Get the organization object for org_id if it exists, otherwise return the default organization object"""
        if org_id is None:
            org_id = self.organization_manager.DEFAULT_ORG_ID

        try:
            return self.organization_manager.get_organization_by_id(org_id=org_id)
        except NoResultFound:
            raise HTTPException(status_code=404, detail=f"Organization with id {org_id} not found")

    def list_llm_models(self) -> List[LLMConfig]:
        """List available models"""

        llm_models = []
        for provider in self._enabled_providers:
            try:
                llm_models.extend(provider.list_llm_models())
            except Exception as e:
                warnings.warn(f"An error occurred while listing LLM models for provider {provider}: {e}")
        return llm_models

    def list_embedding_models(self) -> List[EmbeddingConfig]:
        """List available embedding models"""
        embedding_models = []
        for provider in self._enabled_providers:
            try:
                embedding_models.extend(provider.list_embedding_models())
            except Exception as e:
                warnings.warn(f"An error occurred while listing embedding models for provider {provider}: {e}")
        return embedding_models

    def add_llm_model(self, request: LLMConfig) -> LLMConfig:
        """Add a new LLM model"""

    def add_embedding_model(self, request: EmbeddingConfig) -> EmbeddingConfig:
        """Add a new embedding model"""

    def get_agent_context_window(
        self,
        user_id: str,
        agent_id: str,
    ) -> ContextWindowOverview:
        # TODO: Thread actor directly through this function, since the top level caller most likely already retrieved the user
        actor = self.user_manager.get_user_or_default(user_id=user_id)

        # Get the current message
        letta_agent = self.load_agent(agent_id=agent_id, actor=actor)
        return letta_agent.get_context_window()

    def run_tool_from_source(
        self,
        user_id: str,
        tool_args: str,
        tool_source: str,
        tool_source_type: Optional[str] = None,
        tool_name: Optional[str] = None,
    ) -> FunctionReturn:
        """Run a tool from source code"""

        try:
            tool_args_dict = json.loads(tool_args)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON string for tool_args")

        if tool_source_type is not None and tool_source_type != "python":
            raise ValueError("Only Python source code is supported at this time")

        # NOTE: we're creating a floating Tool object and NOT persiting to DB
        tool = Tool(
            name=tool_name,
            source_code=tool_source,
        )
        assert tool.name is not None, "Failed to create tool object"

        # TODO eventually allow using agent state in tools
        agent_state = None

        # Next, attempt to run the tool with the sandbox
        try:
            sandbox_run_result = ToolExecutionSandbox(tool.name, tool_args_dict, user_id, tool_object=tool).run(agent_state=agent_state)
            function_response = str(sandbox_run_result.func_return)
            stdout = [s for s in sandbox_run_result.stdout if s.strip()]
            stderr = [s for s in sandbox_run_result.stderr if s.strip()]

            # expected error
            if stderr:
                error_msg = self.get_error_msg_for_func_return(tool.name, stderr[-1])
                return FunctionReturn(
                    id="null",
                    function_call_id="null",
                    date=get_utc_time(),
                    status="error",
                    function_return=error_msg,
                    stdout=stdout,
                    stderr=stderr,
                )

            return FunctionReturn(
                id="null",
                function_call_id="null",
                date=get_utc_time(),
                status="success",
                function_return=function_response,
                stdout=stdout,
                stderr=stderr,
            )

        # unexpected error TODO(@cthomas): consolidate error handling
        except Exception as e:
            error_msg = self.get_error_msg_for_func_return(tool.name, e)
            return FunctionReturn(
                id="null",
                function_call_id="null",
                date=get_utc_time(),
                status="error",
                function_return=error_msg,
                stdout=[""],
                stderr=[traceback.format_exc()],
            )

    def get_error_msg_for_func_return(self, tool_name, exception_message):
        # same as agent.py
        from letta.constants import MAX_ERROR_MESSAGE_CHAR_LIMIT

        error_msg = f"Error executing tool {tool_name}: {exception_message}"
        if len(error_msg) > MAX_ERROR_MESSAGE_CHAR_LIMIT:
            error_msg = error_msg[:MAX_ERROR_MESSAGE_CHAR_LIMIT]
        return error_msg

    # Composio wrappers
    def get_composio_client(self, api_key: Optional[str] = None):
        if api_key:
            return Composio(api_key=api_key)
        elif tool_settings.composio_api_key:
            return Composio(api_key=tool_settings.composio_api_key)
        else:
            return Composio()

    def get_composio_apps(self, api_key: Optional[str] = None) -> List["AppModel"]:
        """Get a list of all Composio apps with actions"""
        apps = self.get_composio_client(api_key=api_key).apps.get()
        apps_with_actions = []
        for app in apps:
            # A bit of hacky logic until composio patches this
            if app.meta["actionsCount"] > 0 and not app.name.lower().endswith("_beta"):
                apps_with_actions.append(app)

        return apps_with_actions

    def get_composio_actions_from_app_name(self, composio_app_name: str, api_key: Optional[str] = None) -> List["ActionModel"]:
        actions = self.get_composio_client(api_key=api_key).actions.get(apps=[composio_app_name])
        return actions
