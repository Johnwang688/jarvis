# Jarvis HUD terminal startup file for any shell but bash (WP-C; HUD plan
# §2.4, decisions W-2 and W-5). bash gets terminal_rc.bash instead. POSIX
# shell only: a dash parses every line of this file.
#
# The daemon reads this file once, puts one line in front of it that sets
# __jarvis_nonce, and writes the result for each terminal into a private
# directory (mode 700). The shell starts as a login shell (-l), so it reads
# /etc/profile and ~/.profile itself, and then this copy as $ENV. Two things:
#
# 1. sudo never caches in a HUD terminal (W-5): `sudo -k cmd` ignores the
#    cached credential and does not refresh it.
#
# 2. The prompt carries OSC 133 A and B marks (prompt start, prompt end),
#    signed with the per-terminal nonce. A POSIX shell has no hook before a
#    command runs, so there are no C or D marks and no per-command spans
#    here: a reader of such a terminal falls back to matching command lines
#    in the text (W-2, item 3).

alias sudo='sudo -k'

__jarvis_esc=$(printf '\033')
PS1="${__jarvis_esc}]133;A;jarvis=${__jarvis_nonce}${__jarvis_esc}\\${PS1-\$ }${__jarvis_esc}]133;B;jarvis=${__jarvis_nonce}${__jarvis_esc}\\"
unset __jarvis_esc
