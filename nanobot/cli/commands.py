"""
nanobot 命令行接口（CLI）命令定义

这是整个 nanobot 项目的入口模块，定义了所有 CLI 命令：
- onboard: 初始化配置和工作区
- gateway: 启动网关服务（多频道模式）
- agent: 与 Agent 交互（CLI 模式）
- status: 查看状态
- channels: 频道管理
- provider: 提供商管理
"""

import asyncio
import os
import select
import signal
import sys
from pathlib import Path

import typer  # CLI 框架（类似 Java 的 Picocli）
from prompt_toolkit import PromptSession  # 交互式输入库
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory  # 命令历史持久化
from prompt_toolkit.patch_stdout import patch_stdout  # 修复输出显示
from rich.console import Console  # 终端美化库
from rich.markdown import Markdown  # Markdown 渲染
from rich.table import Table  # 表格渲染
from rich.text import Text  # 文本渲染

from nanobot import __logo__, __version__  # Logo 和版本号
from nanobot.config.schema import Config  # 配置模型
from nanobot.utils.helpers import sync_workspace_templates  # 同步工作区模板

# 创建 Typer 应用实例（类似 Spring 的 ApplicationContext）
app = typer.Typer(
    name="nanobot",
    help=f"{__logo__} nanobot - Personal AI Assistant",
    no_args_is_help=True,  # 无参数时显示帮助信息
)

# 创建控制台对象（用于美化输出）
console = Console()

# 定义退出命令集合（用于交互式模式）
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI 输入处理：使用 prompt_toolkit 实现编辑、粘贴、历史记录和显示功能
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None  # 全局输入会话对象
_SAVED_TERM_ATTRS = None  # 保存终端原始属性，退出时恢复


def _flush_pending_tty_input() -> None:
    """
    丢弃用户在 AI 生成回复期间输入的按键（防止输入堆积）
    
    当 Agent 正在思考时，用户可能会无意识地输入一些字符，
    这个函数会清空这些未读的输入，避免干扰后续交互。
    """
    try:
        fd = sys.stdin.fileno()  # 获取标准输入文件描述符
        if not os.isatty(fd):  # 如果不是终端设备（如管道），则跳过
            return
    except Exception:
        return

    # 方法 1：使用 termios 清空输入缓冲区（Unix/Linux/Mac）
    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)  # 丢弃输入队列中的数据
        return
    except Exception:
        pass

    # 方法 2：手动读取并丢弃输入（备用方案）
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)  # 检查是否有可读数据
            if not ready:
                break
            if not os.read(fd, 4096):  # 读取并丢弃
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """
    恢复终端到原始状态（回显、行缓冲等）
    
    在程序退出时调用，确保终端设置不会被破坏。
    """
    if _SAVED_TERM_ATTRS is None:  # 如果没有保存过终端属性，则跳过
        return
    try:
        import termios
        # 将终端属性恢复为保存的值
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """
    创建 prompt_toolkit 会话，支持持久化文件历史记录
    
    这个会话提供：
    - 命令历史持久化（类似 bash 的 history）
    - 上下箭头导航历史命令
    - 输入编辑功能
    """
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # 保存终端当前配置，以便退出时恢复
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    # 创建历史文件路径：~/.nanobot/history/cli_history
    history_file = Path.home() / ".nanobot" / "history" / "cli_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)  # 确保目录存在

    # 创建会话对象
    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),  # 使用文件存储历史
        enable_open_in_editor=False,  # 禁用打开编辑器
        multiline=False,   # 单行模式（Enter 键提交）
    )


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """
    使用一致的终端样式渲染 Agent 响应
    
    Args:
        response: Agent 的回复内容
        render_markdown: 是否将内容渲染为 Markdown 格式
    """
    content = response or ""  # 处理空响应
    # 根据参数选择渲染方式：Markdown 或纯文本
    body = Markdown(content) if render_markdown else Text(content)
    console.print()  # 空行
    console.print(f"[cyan]{__logo__} nanobot[/cyan]")  # 显示 Logo
    console.print(body)  # 显示内容
    console.print()  # 空行


def _is_exit_command(command: str) -> bool:
    """
    判断输入是否为退出命令
    
    Args:
        command: 用户输入的命令
        
    Returns:
        True 如果是退出命令，否则 False
    """
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """
    使用 prompt_toolkit 读取用户输入（支持粘贴、历史记录、美化显示）

    prompt_toolkit 原生支持：
    - 多行粘贴（括号粘贴模式）
    - 历史记录导航（上下箭头）
    - 清洁显示（无重影字符或伪影）
    
    Returns:
        用户输入的字符串
        
    Raises:
        RuntimeError: 如果未初始化会话
        KeyboardInterrupt: 如果遇到 EOF 错误
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        # 使用 patch_stdout 确保输出不会被 prompt_toolkit 干扰
        with patch_stdout():
            # 显示蓝色提示符 "You: "
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        # 将 EOF 错误转换为键盘中断
        raise KeyboardInterrupt from exc



def version_callback(value: bool):
    """
    版本回调函数：当用户传入 --version 时显示版本号并退出
    
    Args:
        value: --version 参数的值
    """
    if value:
        console.print(f"{__logo__} nanobot v{__version__}")
        raise typer.Exit()  # 退出程序


@app.callback()  # 装饰器：定义主命令的回调（类似 Java 的 @Command）
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """
    nanobot - 个人 AI 助手主入口
    
    Typer 会自动生成 --help 和 --version 选项
    """
    pass


# ============================================================================
# Onboard / Setup - 初始化配置和工作区
# ============================================================================


@app.command()  # 注册子命令：nanobot onboard
def onboard():
    """
    初始化 nanobot 配置和工作区
    
    这是用户首次使用 nanobot 时必须运行的命令，会：
    1. 创建配置文件 ~/.nanobot/config.json
    2. 创建工作区目录
    3. 同步模板文件到工作区
    """
    # 局部导入：只在需要时导入，减少启动时间
    from nanobot.config.loader import get_config_path, load_config, save_config
    from nanobot.config.schema import Config
    from nanobot.utils.helpers import get_workspace_path

    # 获取配置文件路径
    config_path = get_config_path()

    # 检查配置文件是否已存在
    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        console.print("  [bold]y[/bold] = 覆盖为默认值（现有值将丢失）")
        console.print("  [bold]N[/bold] = 刷新配置，保留现有值并添加新字段")
        if typer.confirm("Overwrite?"):  # 交互式确认
            config = Config()  # 创建默认配置
            save_config(config)
            console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
        else:
            config = load_config()  # 加载现有配置
            save_config(config)  # 重新保存（会添加新版本字段）
            console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        # 创建新配置
        save_config(Config())
        console.print(f"[green]✓[/green] Created config at {config_path}")

    # 创建工作区目录
    workspace = get_workspace_path()

    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)  # 递归创建目录
        console.print(f"[green]✓[/green] Created workspace at {workspace}")

    # 同步模板文件到工作区（AGENTS.md, SOUL.md 等）
    sync_workspace_templates(workspace)

    # 显示成功消息
    console.print(f"\n{__logo__} nanobot is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.nanobot/config.json[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print("  2. Chat: [cyan]nanobot agent -m \"Hello!\"[/cyan]")
    console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/nanobot#-chat-apps[/dim]")





def _make_provider(config: Config):
    """
    工厂方法：根据配置创建合适的 LLM Provider 实例
    
    这是工厂模式的实现，根据模型名称或提供商名称返回不同的 Provider：
    1. OpenAI Codex（OAuth 认证）
    2. Custom Provider（直接调用 OpenAI 兼容接口）
    3. LiteLLM Provider（通过 LiteLLM 支持 20+ 模型）
    
    Args:
        config: 配置对象
        
    Returns:
        LLMProvider 实例
        
    Raises:
        typer.Exit: 如果没有配置 API Key
    """
    from nanobot.providers.custom_provider import CustomProvider
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider

    # 获取默认模型名称
    model = config.agents.defaults.model
    # 获取提供商名称（自动匹配）
    provider_name = config.get_provider_name(model)
    # 获取提供商配置
    p = config.get_provider(model)

    # 情况 1: OpenAI Codex（OAuth 认证，不需要 API Key）
    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        return OpenAICodexProvider(default_model=model)

    # 情况 2: Custom Provider（直接调用 OpenAI 兼容接口，绕过 LiteLLM）
    if provider_name == "custom":
        return CustomProvider(
            api_key=p.api_key if p else "no-key",  # 如果没有配置则使用占位符
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",  # 默认本地地址
            default_model=model,
        )

    # 情况 3: 标准 Provider（通过 LiteLLM）
    from nanobot.providers.registry import find_by_name
    spec = find_by_name(provider_name)
    
    # 检查 API Key 配置（Bedrock 和 OAuth 除外）
    if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and spec.is_oauth):
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.nanobot/config.json under providers section")
        raise typer.Exit(1)

    # 创建 LiteLLM Provider
    return LiteLLMProvider(
        api_key=p.api_key if p else None,  # 如果有配置则使用
        api_base=config.get_api_base(model),  # 自定义 API 地址
        default_model=model,
        extra_headers=p.extra_headers if p else None,  # 自定义请求头
        provider_name=provider_name,
    )


# ============================================================================
# Gateway / Server - 启动网关服务（多频道模式）
# ============================================================================


@app.command()  # 注册子命令：nanobot gateway
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="网关端口"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    config: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出详细日志"),
):
    """
    启动 nanobot 网关服务
    
    这是生产环境使用的模式，会启动：
    - 所有启用的频道（Telegram、Discord、微信等）
    - Agent 核心循环
    - 定时任务服务
    - 心跳服务
    - 消息总线
    """
    # 导入核心组件
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.loader import load_config
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob
    from nanobot.heartbeat.service import HeartbeatService
    from nanobot.session.manager import SessionManager

    # 如果启用详细日志，配置 logging
    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    # 加载配置
    config_path = Path(config) if config else None
    config = load_config(config_path)
    if workspace:
        config.agents.defaults.workspace = workspace

    console.print(f"{__logo__} Starting nanobot gateway on port {port}...")
    # 同步工作区模板
    sync_workspace_templates(config.workspace_path)
    
    # 1️⃣ 创建消息总线（解耦频道与 Agent）
    bus = MessageBus()
    # 2️⃣ 创建 LLM Provider
    provider = _make_provider(config)
    # 3️⃣ 创建会话管理器
    session_manager = SessionManager(config.workspace_path)

    # 4️⃣ 创建定时任务服务
    # 使用工作区路径存储定时任务（每个实例独立）
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # 5️⃣ 创建 Agent 核心循环（依赖注入）
    agent = AgentLoop(
        bus=bus,                    # 消息总线
        provider=provider,          # LLM Provider
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        brave_api_key=config.tools.web.search.api_key or None,  # 搜索 API Key
        web_proxy=config.tools.web.proxy or None,  # Web 代理
        exec_config=config.tools.exec,  # Shell 执行配置
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,  # 限制在工作区
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,  # MCP 服务器配置
        channels_config=config.channels,  # 频道配置
    )

    # 6️⃣ 设置定时任务回调（需要 agent 实例）
    async def on_cron_job(job: CronJob) -> str | None:
        """
        通过 Agent 执行定时任务
        
        Args:
            job: 定时任务对象
            
        Returns:
            执行结果或 None
        """
        from nanobot.agent.tools.cron import CronTool
        from nanobot.agent.tools.message import MessageTool
        
        # 构建提醒消息
        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        # 防止定时任务执行期间递归创建新任务
        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)  # 设置上下文标记
        try:
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            # 恢复上下文
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        # 检查消息工具是否已发送（避免重复发送）
        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        # 如果需要发送且目标存在，则通过消息总线发送
        if job.payload.deliver and job.payload.to and response:
            from nanobot.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=response
            ))
        return response
    
    cron.on_job = on_cron_job  # 注册回调

    # 7️⃣ 创建频道管理器
    channels = ChannelManager(config, bus)

    # 8️⃣ 选择心跳消息的目标频道/聊天
    def _pick_heartbeat_target() -> tuple[str, str]:
        """
        为心跳触发的消息选择一个可路由的频道/聊天目标
        
        Returns:
            (channel, chat_id) 元组
        """
        enabled = set(channels.enabled_channels)
        # 优先选择最近更新的非内部会话
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:  # 跳过内部频道
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # 降级方案：使用 CLI
        return "cli", "direct"

    # 9️⃣ 创建心跳服务回调
    async def on_heartbeat_execute(tasks: str) -> str:
        """
        第二阶段：通过完整的 Agent 循环执行心跳任务
        
        Args:
            tasks: 心跳任务描述
            
        Returns:
            执行结果
        """
        channel, chat_id = _pick_heartbeat_target()

        # 静默进度回调（不显示进度）
        async def _silent(*_args, **_kwargs):
            pass

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

    async def on_heartbeat_notify(response: str) -> None:
        """
        将心跳响应发送给用户频道
        
        Args:
            response: Agent 响应
        """
        from nanobot.bus.events import OutboundMessage
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # 没有外部频道时不发送
        await bus.publish_outbound(OutboundMessage(
            channel=channel, 
            chat_id=chat_id, 
            content=response
        ))

    # 获取心跳配置
    hb_cfg = config.gateway.heartbeat
    # 创建心跳服务
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
    )

    # 显示启动信息
    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    # 🔟 启动所有服务的异步函数
    async def run():
        try:
            await cron.start()           # 启动定时任务
            await heartbeat.start()      # 启动心跳
            # 并发运行 Agent 和所有频道
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            # 清理资源
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    # 启动异步事件循环
    asyncio.run(run())




# ============================================================================
# Agent Commands - Agent 交互命令
# ============================================================================


@app.command()  # 注册子命令：nanobot agent
def agent(
    message: str = typer.Option(None, "--message", "-m", help="发送给 Agent 的消息"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="会话 ID"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="将输出渲染为 Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="显示运行时日志"),
):
    """
    与 Agent 直接交互
    
    支持两种模式：
    1. 单消息模式：nanobot agent -m "你好"
    2. 交互式模式：nanobot agent（进入 REPL）
    """
    from loguru import logger

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import get_data_dir, load_config
    from nanobot.cron.service import CronService

    # 加载配置
    config = load_config()
    # 同步工作区模板
    sync_workspace_templates(config.workspace_path)

    # 创建消息总线
    bus = MessageBus()
    # 创建 LLM Provider
    provider = _make_provider(config)

    # 创建定时任务服务（CLI 模式不需要回调）
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # 配置日志
    if logs:
        logger.enable("nanobot")  # 启用日志
    else:
        logger.disable("nanobot")  # 禁用日志（显示动画）

    # 创建 Agent 循环
    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        brave_api_key=config.tools.web.search.api_key or None,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
    )

    # 显示"思考中"动画的上下文管理器
    def _thinking_ctx():
        """
        根据日志设置返回不同的上下文：
        - 显示日志时：不显示动画（避免干扰）
        - 不显示日志时：显示旋转动画
        """
        if logs:
            from contextlib import nullcontext
            return nullcontext()  # 空上下文
        # 显示"nanobot is thinking..."动画
        return console.status("[dim]nanobot is thinking...[/dim]", spinner="dots")

    # 进度回调函数
    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        """
        显示 Agent 执行进度
        
        Args:
            content: 进度内容
            tool_hint: 是否是工具提示
        """
        ch = agent_loop.channels_config
        # 根据配置决定是否显示
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        console.print(f"  [dim]↳ {content}[/dim]")

    # 🟢 模式 1：单消息模式（有 -m 参数）
    if message:
        async def run_once():
            """执行单次请求"""
            with _thinking_ctx():
                response = await agent_loop.process_direct(message, session_id, on_progress=_cli_progress)
            _print_agent_response(response, render_markdown=markdown)
            await agent_loop.close_mcp()  # 清理 MCP 资源

        asyncio.run(run_once())
    
    # 🟢 模式 2：交互式模式（无参数）
    else:
        from nanobot.bus.events import InboundMessage
        # 初始化输入会话
        _init_prompt_session()
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        # 解析 session_id（格式：channel:chat_id）
        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        # 信号处理器（处理 Ctrl+C 等）
        def _handle_signal(signum, frame):
            """处理系统信号，优雅退出"""
            sig_name = signal.Signals(signum).name
            _restore_terminal()  # 恢复终端
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        # 注册信号处理器
        signal.signal(signal.SIGINT, _handle_signal)   # Ctrl+C
        signal.signal(signal.SIGTERM, _handle_signal)  # kill 命令
        signal.signal(signal.SIGHUP, _handle_signal)   # 终端断开
        # 忽略 SIGPIPE（防止写入关闭的管道时静默退出）
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            """交互式模式主循环"""
            # 启动 Agent 循环（后台任务）
            bus_task = asyncio.create_task(agent_loop.run())
            # 创建事件标记（用于等待响应）
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[str] = []

            async def _consume_outbound():
                """消费出站消息（后台任务）"""
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            # 进度消息
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                console.print(f"  [dim]↳ {msg.content}[/dim]")
                        elif not turn_done.is_set():
                            # 当前回合的响应
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()  # 标记完成
                        elif msg.content:
                            # 后续消息（直接显示）
                            console.print()
                            _print_agent_response(msg.content, render_markdown=markdown)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            # 启动出站消息消费任务
            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                # 主循环：读取用户输入 → 发送 → 等待响应
                while True:
                    try:
                        _flush_pending_tty_input()  # 清空未读输入
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        # 检查退出命令
                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        # 重置回合标记
                        turn_done.clear()
                        turn_response.clear()

                        # 发布消息到总线
                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                        ))

                        # 等待响应
                        with _thinking_ctx():
                            await turn_done.wait()

                        # 显示响应
                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                # 清理资源
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        # 启动交互式循环
        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands - 频道管理命令
# ============================================================================


# 创建子命令组：nanobot channels <command>
channels_app = typer.Typer(help="管理频道")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")  # nanobot channels status
def channels_status():
    """
    显示所有频道的状态
    
    以表格形式展示：
    - 频道名称
    - 是否启用
    - 配置信息
    """
    from nanobot.config.loader import load_config

    config = load_config()

    # 创建表格
    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # WhatsApp 配置
    wa = config.channels.whatsapp
    table.add_row(
        "WhatsApp",
        "✓" if wa.enabled else "✗",  # 启用状态
        wa.bridge_url  # Bridge 地址
    )

    # Discord 配置
    dc = config.channels.discord
    table.add_row(
        "Discord",
        "✓" if dc.enabled else "✗",
        dc.gateway_url  # 网关地址
    )

    # 飞书配置
    fs = config.channels.feishu
    fs_config = f"app_id: {fs.app_id[:10]}..." if fs.app_id else "[dim]not configured[/dim]"
    table.add_row(
        "Feishu",
        "✓" if fs.enabled else "✗",
        fs_config  # 显示 AppID 前 10 位
    )

    # Mochat 配置
    mc = config.channels.mochat
    mc_base = mc.base_url or "[dim]not configured[/dim]"
    table.add_row(
        "Mochat",
        "✓" if mc.enabled else "✗",
        mc_base  # 基础 URL
    )

    # Telegram 配置
    tg = config.channels.telegram
    tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
    table.add_row(
        "Telegram",
        "✓" if tg.enabled else "✗",
        tg_config  # 显示 Token 前 10 位
    )

    # Slack 配置
    slack = config.channels.slack
    slack_config = "socket" if slack.app_token and slack.bot_token else "[dim]not configured[/dim]"
    table.add_row(
        "Slack",
        "✓" if slack.enabled else "✗",
        slack_config  # Socket Mode
    )

    # 钉钉配置
    dt = config.channels.dingtalk
    dt_config = f"client_id: {dt.client_id[:10]}..." if dt.client_id else "[dim]not configured[/dim]"
    table.add_row(
        "DingTalk",
        "✓" if dt.enabled else "✗",
        dt_config  # 显示 ClientID 前 10 位
    )

    # QQ 配置
    qq = config.channels.qq
    qq_config = f"app_id: {qq.app_id[:10]}..." if qq.app_id else "[dim]not configured[/dim]"
    table.add_row(
        "QQ",
        "✓" if qq.enabled else "✗",
        qq_config  # 显示 AppID 前 10 位
    )

    # Email 配置
    em = config.channels.email
    em_config = em.imap_host if em.imap_host else "[dim]not configured[/dim]"
    table.add_row(
        "Email",
        "✓" if em.enabled else "✗",
        em_config  # IMAP 服务器
    )

    console.print(table)  # 打印表格


def _get_bridge_dir() -> Path:
    """
    获取 Bridge 目录，如果未构建则进行设置
    
    Bridge 是 WhatsApp 频道所需的 TypeScript 中间件
    
    Returns:
        Bridge 目录路径
        
    Raises:
        typer.Exit: 如果 npm 未安装或构建失败
    """
    import shutil
    import subprocess

    # 用户 Bridge 目录：~/.nanobot/bridge
    user_bridge = Path.home() / ".nanobot" / "bridge"

    # 检查是否已构建完成
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # 检查 npm 是否可用
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # 查找 Bridge 源码：先检查包内，再检查源码目录
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # 安装后的位置
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # 开发时的位置

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall nanobot")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # 复制源码到用户目录
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)  # 先删除旧版本
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # 安装依赖并构建
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")  # 显示错误前 500 字符
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")  # nanobot channels login
def channels_login():
    """
    通过二维码连接设备（WhatsApp 专用）
    
    会启动 Bridge 服务并显示二维码，用户需要用手机 WhatsApp 扫描
    """
    import subprocess

    from nanobot.config.loader import load_config

    config = load_config()
    bridge_dir = _get_bridge_dir()  # 获取或构建 Bridge

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")  # 提示用户扫描二维码

    # 设置环境变量
    env = {**os.environ}
    if config.channels.whatsapp.bridge_token:
        env["BRIDGE_TOKEN"] = config.channels.whatsapp.bridge_token

    # 启动 Bridge
    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Status Commands - 状态查看命令
# ============================================================================


@app.command()  # nanobot status
def status():
    """
    显示 nanobot 状态信息
    
    包括：
    - 配置文件状态
    - 工作区状态
    - 当前模型
    - 各 Provider 的 API Key 配置
    """
    from nanobot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} nanobot Status\n")

    # 显示配置和工作区状态
    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from nanobot.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        # 检查各 Provider 的 API Key 配置
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                # OAuth 认证（不需要 API Key）
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # 本地部署（显示 API 地址而不是 Key）
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                # 标准 API Key 认证
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


# ============================================================================
# OAuth Login - OAuth 登录认证
# ============================================================================

# 创建子命令组：nanobot provider <command>
provider_app = typer.Typer(help="管理 Provider")
app.add_typer(provider_app, name="provider")


# 存储登录处理器的注册表
_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    """
    装饰器：注册 Provider 登录处理器
    
    Args:
        name: Provider 名称
        
    Returns:
        装饰器函数
    """
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn  # 注册处理器
        return fn
    return decorator


@provider_app.command("login")  # nanobot provider login
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider 名称（如 'openai-codex', 'github-copilot'）"),
):
    """
    与 OAuth Provider 进行认证
    
    支持：
    - openai-codex: OpenAI Codex（OAuth）
    - github-copilot: GitHub Copilot（设备流）
    """
    from nanobot.providers.registry import PROVIDERS

    # 将名称转换为内部格式（如 'openai-codex' → 'openai_codex'）
    key = provider.replace("-", "_")
    # 查找 Provider 规格
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        # 显示支持的 Provider 列表
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    # 获取登录处理器
    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()  # 调用处理器


@_register_login("openai_codex")  # 注册 OpenAI Codex 登录处理器
def _login_openai_codex() -> None:
    """
    OpenAI Codex OAuth 登录
    
    使用 oauth_cli_kit 库进行交互式认证
    """
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()  # 尝试获取已保存的 Token
        except Exception:
            pass
        if not (token and token.access):
            # 没有有效 Token，启动交互式登录
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),  # 输出函数
                prompt_fn=lambda s: typer.prompt(s),  # 输入提示函数
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")  # 注册 GitHub Copilot 登录处理器
def _login_github_copilot() -> None:
    """
    GitHub Copilot 设备流认证
    
    通过发送一个简单的请求触发 GitHub 的设备认证流程
    """
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion
        # 发送一个简单的请求，触发认证流程
        await acompletion(model="github_copilot/gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


# 文件入口：当直接运行此文件时启动 CLI
if __name__ == "__main__":
    app()


if __name__ == "__main__":
    app()
