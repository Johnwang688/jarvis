# Jarvis HUD terminal startup file for a POSIX sh-family shell (sh, dash, ash,
# ksh, mksh, …; WP-C, HUD plan §2.4, decisions W-2 and W-5). bash gets
# terminal_rc.bash instead, and a shell that does not read $ENV (zsh, fish)
# gets no startup file at all. POSIX shell only: a dash parses every line.
#
# The daemon reads this file once, puts one line in front of it that sets
# __jarvis_nonce, and writes the result for each terminal into a pipe, never
# to disk. The shell starts interactive (not as a login shell) with $ENV
# naming /dev/fd/<n>; its first read drains the pipe, whose write end is
# already closed, so anything that opens it later — even through the
# shell's own descriptor while the login files below run — reads nothing.
# Its first act is to unset ENV. The nonce is then left only in an
# unexported shell variable, which PS1 names rather than holds (below).
# Then:
#
# 1. What a login shell would have read: /etc/profile, then ~/.profile.
#
# 2. sudo never caches in a HUD terminal (W-5): `sudo -k cmd` ignores the
#    cached credential and does not refresh it.
#
# 3. The prompt carries OSC 133 A and B marks (prompt start, prompt end),
#    signed with the nonce and ended with BEL (dash expands PS1, and would
#    eat the backslash of an ESC \ terminator). A POSIX shell has no hook
#    before a command runs, so there are no C or D marks and no per-command
#    spans here: a reader of such a terminal has text patterns only (W-2,
#    item 3).

if [ -f "$ENV" ]; then rm -f -- "$ENV"; fi     # a file, if ever one is used: gone
unset ENV

if [ -r /etc/profile ]; then . /etc/profile; fi
if [ -r "$HOME/.profile" ]; then . "$HOME/.profile"; fi

alias sudo='sudo -k'

__jarvis_esc=$(printf '\033')
__jarvis_bel=$(printf '\007')
__jarvis_ps1=${PS1-'$ '}
# PS1 holds the *name* ${__jarvis_nonce}, expanded at each prompt, never the
# value: the dotfiles may have exported PS1 (and dash cannot un-export a
# variable), so whatever a child inherits as PS1 carries no nonce.
unset PS1
PS1="${__jarvis_esc}]133;A;jarvis="'${__jarvis_nonce}'"${__jarvis_bel}${__jarvis_ps1}${__jarvis_esc}]133;B;jarvis="'${__jarvis_nonce}'"${__jarvis_bel}"
unset __jarvis_esc __jarvis_bel __jarvis_ps1
