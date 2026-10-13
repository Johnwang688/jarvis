# Exact prompt paragraph narrowing

The PR #42 follow-ups are fixed by matching complete canonical paragraphs, ignoring only surrounding newlines. A caller paragraph sharing a rule's opening, appending a sentence, or changing whitespace is retained byte for byte. Canonical paragraphs still narrow to the existing versions; full toolsets keep the complete system prompt unchanged. The workflow Git paragraph has one shared constant, preserving its original wording. Startup refuses an ambiguous or missing canonical rule.

The sentence audit lists complete whitespace-normalized dropped sentences. A future edit extending one of those sentences must be reflected in narrower versions or explicitly reviewed; sharing a prefix no longer exempts it.

Free validation: `tests/prompt_tools_check.py` covers every existing surface, caller collisions and exact original behavior. Independent review and full lead sweep are required before merge. This changes prompt matching, not executable permission policy.
