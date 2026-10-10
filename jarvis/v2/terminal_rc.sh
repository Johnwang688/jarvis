# Jarvis HUD terminal startup file (WP-C; HUD plan §2.4, decisions W-2 and W-5).
#
# The daemon reads this file once, puts one line in front of it that sets
# __jarvis_nonce, and writes the result for each terminal into a private
# directory (mode 700). The shell is handed that copy: bash reads it as its
# --rcfile, any other shell as $ENV. It does three things and nothing else.
#
# 1. bash only: what a login shell would have read. --rcfile makes bash an
#    interactive *non-login* shell, so /etc/profile and the first of
#    ~/.bash_profile, ~/.bash_login and ~/.profile are read here, exactly as a
#    fresh WSL tab reads them (and Ubuntu's ~/.profile reads ~/.bashrc).
#
# 2. sudo never caches in a HUD terminal (W-5). `sudo -k cmd` ignores the
#    cached credential and does not refresh it, so every sudo asks for the
#    password, and a line injected by a local program cannot ride a sudo the
#    owner just typed.
#
# 3. Shell-integration marks (OSC 133), so the daemon knows which output
#    belongs to which command line (W-2, item 3):
#      ESC ] 133 ; A ; jarvis=<nonce> ESC \        a prompt starts
#      ESC ] 133 ; B ; jarvis=<nonce> ESC \        the prompt ends, input starts
#      ESC ] 133 ; C ; cmdline_url=<%-encoded line> ; jarvis=<nonce> ESC \
#                                                  the command runs: output starts
#      ESC ] 133 ; D ; <exit status> ; jarvis=<nonce> ESC \
#                                                  the command ended
#    The nonce is per terminal and never exported, so a program's output
#    cannot forge a mark the daemon believes. Only bash gets C and D (they
#    need a DEBUG trap and PROMPT_COMMAND); other shells get A and B.
#
#    The command line comes from `history 1`. When the line did not enter the
#    history (HISTCONTROL=ignorespace, a duplicate under ignoredups, history
#    off), it is bash's $BASH_COMMAND instead: the first simple command of the
#    line, not the whole line.

if [ -n "${BASH_VERSION:-}" ]; then
    if [ -r /etc/profile ]; then . /etc/profile; fi
    if [ -r "$HOME/.bash_profile" ]; then . "$HOME/.bash_profile"
    elif [ -r "$HOME/.bash_login" ]; then . "$HOME/.bash_login"
    elif [ -r "$HOME/.profile" ]; then . "$HOME/.profile"
    fi
fi

alias sudo='sudo -k'

if [ -n "${BASH_VERSION:-}" ]; then
    __jarvis_mark() {
        builtin printf '\033]133;%s;jarvis=%s\033\\' "$1" "$__jarvis_nonce"
    }

    # Percent-encode $1 byte by byte into __jarvis_encoded (no subshell), so a
    # mark can carry any command line without an ESC, BEL or ';' inside it.
    __jarvis_urlencode() {
        local LC_ALL=C s="$1" c i
        __jarvis_encoded=
        for (( i = 0; i < ${#s}; i++ )); do
            c=${s:i:1}
            case $c in
                [a-zA-Z0-9.~_/-]) __jarvis_encoded+=$c ;;
                *) builtin printf -v c '%%%02X' "'$c"; __jarvis_encoded+=$c ;;
            esac
        done
    }

    __jarvis_histnum() {
        local entry
        entry=$(HISTTIMEFORMAT= builtin history 1 2>/dev/null)
        if [[ $entry =~ ^[[:space:]]*([0-9]+) ]]; then
            __jarvis_hist=${BASH_REMATCH[1]}
        else
            __jarvis_hist=
        fi
    }

    __jarvis_state=start        # start | prompt | ready | running
    __jarvis_ran=
    __jarvis_status=0
    __jarvis_hist=

    # First in PROMPT_COMMAND: the status of the command that just ended,
    # before anything else in PROMPT_COMMAND can change $?.
    __jarvis_capture() {
        __jarvis_status=$?
        __jarvis_ran=$__jarvis_state
        __jarvis_state=prompt
    }

    # Last in PROMPT_COMMAND, so it sees the PS1 every other hook has built.
    __jarvis_precmd() {
        if [ "$__jarvis_ran" = running ]; then
            __jarvis_mark "D;$__jarvis_status"
        fi
        __jarvis_histnum
        __jarvis_mark A
        case $PS1 in
            *"133;B;jarvis="*) ;;
            *) PS1="$PS1"'\[\033]133;B;jarvis='"$__jarvis_nonce"'\033\\\]' ;;
        esac
        __jarvis_state=ready
    }

    # The DEBUG trap fires before every simple command; only the first one
    # after a prompt is the start of the owner's command line.
    __jarvis_preexec() {
        [ "$__jarvis_state" = ready ] || return 0
        [ -n "${COMP_LINE:-}" ] && return 0
        case $BASH_COMMAND in __jarvis_*) return 0 ;; esac
        __jarvis_state=running
        local entry line=
        entry=$(HISTTIMEFORMAT= builtin history 1 2>/dev/null)
        if [[ $entry =~ ^[[:space:]]*([0-9]+)[*]?[[:space:]]+(.*)$ ]]; then
            if [ "${BASH_REMATCH[1]}" != "$__jarvis_hist" ]; then
                line=${BASH_REMATCH[2]}
            fi
        fi
        [ -n "$line" ] || line=$BASH_COMMAND
        __jarvis_urlencode "$line"
        __jarvis_mark "C;cmdline_url=$__jarvis_encoded"
    }

    # Keep a DEBUG trap the owner's own startup files set, and run it after ours.
    __jarvis_prev_debug=
    __jarvis_third() { __jarvis_prev_debug=$3; }
    __jarvis_trap=$(trap -p DEBUG)
    if [ -n "$__jarvis_trap" ]; then eval "__jarvis_third $__jarvis_trap"; fi
    unset __jarvis_trap
    __jarvis_debug() {
        __jarvis_preexec
        if [ -n "$__jarvis_prev_debug" ]; then eval "$__jarvis_prev_debug"; fi
    }
    trap '__jarvis_debug' DEBUG

    # Newlines, not ';', join the hooks: a PROMPT_COMMAND that already ends
    # in ';' would otherwise become a syntax error.
    if [[ $(declare -p PROMPT_COMMAND 2>/dev/null) == "declare -a"* ]]; then
        PROMPT_COMMAND=(__jarvis_capture "${PROMPT_COMMAND[@]}" __jarvis_precmd)
    else
        PROMPT_COMMAND=$'__jarvis_capture\n'"${PROMPT_COMMAND:-}"$'\n__jarvis_precmd'
    fi
else
    __jarvis_esc=$(printf '\033')
    PS1="${__jarvis_esc}]133;A;jarvis=${__jarvis_nonce}${__jarvis_esc}\\${PS1-\$ }${__jarvis_esc}]133;B;jarvis=${__jarvis_nonce}${__jarvis_esc}\\"
    unset __jarvis_esc
fi
