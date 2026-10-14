# llm-output-reviewer
model: sonnet
verification: mixed
**Checks:** Model output flowing into labels, Gmail actions, SQL, files or rule proposals without validation against a closed set; model/agent calls given tools, credentials or network.
**Spawn when:** diff touches `src/triage/engine/{matcher,variables}.py`, `services/{similarity,teach_bot,worker}.py`, or adds calls to `check_intent`, `check_email_intent`, `extract_value`, `llm_client`, or LLM client code; skip otherwise.

**Proof:** evidence-query (grep every LLM client call and trace its return to a sink, quote the path) and mutation (feed a hostile string through a stub client in a scratch copy and show the resulting label/action); design concerns labelled judgement.

**Your focus:** what does this model's output cause to happen? Email is attacker-controlled; the model returns text only, and Python validates before anything acts. See `docs/agent-isolation.md` in the cortex root.

**What to flag**
1. LLM-extracted string used as a Gmail label name, label ID, rule field or file path without checking membership in a closed set (or a strict charset/length) first.
2. LLM output interpolated into SQL, shell, or a config/YAML write instead of a validated enum/parameter.
3. An LLM call site given tool use, credentials, network or filesystem access, or a prompt that includes secrets or other users' mail.
4. A yes/no intent parser that treats anything but an exact expected token as true (fails open), or that raises on malformed output and drops the queue job.
5. Unbounded email body/subject placed into a prompt without truncation.

**Proof:** trace the call to its sink with grep and quote each hop; for 1/2/4 run a stub client returning `"; DROP TABLE x"` / `"Cortex/Admin"` / `"maybe"` in a scratch copy and report the resulting action.

**Do NOT flag**
- Model name or prompt text hardcoded (homelab-values-reviewer owns it).
- Output already compared against a closed set or an enum.
- Debug logging of truncated LLM values.
- Test stubs.
