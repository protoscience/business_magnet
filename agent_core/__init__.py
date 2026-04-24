"""agent_core — reusable framework for Claude agents on WhatsApp + Discord.

Public API:
    build_options(system_prompt, tools, agent_name=None, ...) -> ClaudeAgentOptions
    run_whatsapp_bridge(build_opts, port=4000)
    run_discord(build_opts)
    active_agent, active_sender   # ContextVars for tool implementations
    IMAGE_MARKER                  # marker tools use to attach generated images
"""

__version__ = "0.1.0"
