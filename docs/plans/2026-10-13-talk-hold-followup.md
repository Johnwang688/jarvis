# Monaco chord hold follow-up

Ctrl+K Ctrl+D belongs to Monaco. Keep its second key marked held so its repeats cannot become shell EOF if focus moves to a terminal before keyup, including after window blur. It never starts speech; D-up or Ctrl-up clears the hold, and a fresh nonrepeat terminal Ctrl+D retains its shell meaning.

Plan independently reviewed by git_wiring. Unit cases cover blur, both release keys and fresh presses; the real Chromium PTT/Monaco fixture crosses focus into a mocked xterm and checks zero bytes followed by a working fresh EOF. No live shell, provider or credential is involved.
