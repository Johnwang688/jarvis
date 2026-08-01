---
description: Use when the owner asks you to build AND ship something to the web — "make a game and deploy it", "put this on vercel", "ship it"
---

Prerequisites (check before starting): `run_readonly` → `gh auth status`
and `vercel whoami`. If either is not logged in, stop and ask the owner to
run `gh auth login` / `vercel login` in their terminal — logins are
human-only, like every credential here.

1. **Build first, deploy second.** Create the project at
   `~/projects/<kebab-name>/` as static html/css/js with `index.html` at
   the root — Vercel serves that as-is, no build step. No frameworks unless
   the owner asked for one.
2. **Verify locally before anything leaves the machine.** Start a
   throwaway server with `run_command`:
   `nohup python3 -m http.server 8410 -d ~/projects/<name> >/dev/null 2>&1 &`
   then `browser_goto` http://localhost:8410, click through it, and check
   the looks with `browser_screenshot`. Fix what's broken now. Afterwards:
   `pkill -f "http.server 8410"`.
3. **Repo — always private.**
   - `git -C ~/projects/<name> init -b main`
   - write a `.gitignore` first: `node_modules`, `.env*`, `.vercel`
   - `git -C ~/projects/<name> add -A` and commit with a real message
   - `gh repo create <name> --private --source ~/projects/<name> --push`
   Public repos only if the owner explicitly said public.
4. **STOP AND ASK — before any deploy.** The Vercel project name decides
   the URL (`<name>.vercel.app`). Ask the owner what name they want. Never
   choose it yourself, never deploy before they answer.
5. **Preview deploy** with their chosen name:
   - `vercel link --yes --cwd ~/projects/<name> --project <chosen-name>`
   - `vercel deploy --yes --cwd ~/projects/<name>`
   Give the owner the preview URL from the output and wait for them to
   look at it.
6. **Production only after they approve the preview**, with a fresh yes
   from this conversation — never assumed:
   `vercel deploy --prod --yes --cwd ~/projects/<name>`
7. **Report**: repo URL, live URL, and the undo (`vercel rollback`, or
   deleting the project in the Vercel dashboard; `gh repo delete` needs the
   owner, not you).

Boundaries: every git/gh/vercel command runs through `run_command` and its
approval gate. Never commit or deploy credentials, tokens, or personal
data. One project per request.
