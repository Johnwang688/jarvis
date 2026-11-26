"""Owner-facing voice problem copy shared by the daemon and Windows helper."""

MICROPHONE = ("Can’t hear you", "The microphone isn’t available.",
              "Another app may be holding it")
UNKNOWN = ("Something went wrong", "Voice hit a problem.",
           "The Voice panel has details.")
_TRY_AGAIN = ("Something went wrong", "Jarvis couldn’t get an answer from Claude.",
              "Try again. The Voice panel has details.")
_NOT_READY = "Voice isn’t ready"

PROBLEMS = {
    "transcriber_unavailable": (_NOT_READY, "Speech recognition isn’t available.",
                                "Run “jarvis voice setup”, then start voice again."),
    "brain_not_signed_in": (_NOT_READY, "Claude isn’t signed in.",
                            "Run “claude” in a terminal and sign in."),
    "brain_cli_missing": (_NOT_READY, "Claude Code wasn’t found.",
                          "Install Claude Code, then restart the agent."),
    "brain_cli_version": (_NOT_READY, "Claude Code is too old.",
                          "Update Claude Code, then restart the agent."),
    "brain_api_key_env": (_NOT_READY, "An API key is set where Jarvis runs.",
                          "Voice uses your Claude sign-in. Remove the key and restart the agent."),
    "brain_private_cwd": (_NOT_READY, "Jarvis’s data folder isn’t private.",
                          "See the Voice panel for details."),
    "brain_connect_timeout": _TRY_AGAIN,
    "brain_connect_failed": _TRY_AGAIN,
    "brain_process_failed": _TRY_AGAIN,
    "brain_empty_response": _TRY_AGAIN,
    "brain_tool_timeout": _TRY_AGAIN,
    "turn_failed": _TRY_AGAIN,
}
