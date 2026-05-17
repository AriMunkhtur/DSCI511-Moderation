"""
prompt_builder.py  (v2 - aligned to policies_v2 schema)

Converts a policy JSON + a post into a single rigidly-structured prompt
string for the local LLM (Ollama).

WHY THIS EXISTS (Palla et al., FAccT 2025):
The paper "Policy-as-Prompt" shows LLM moderation judgments are highly
sensitive to HOW the policy is presented, not just WHAT it says. Key
findings this builder operationalizes:
  1. Structured, enumerated rules outperform prose policy blobs.
     -> _numbered() forces every rule onto its own numbered line.
  2. Explicit allowed/prohibited separation reduces over-removal.
     -> prohibited, allowed, and context_required are SEPARATE sections.
  3. Formatting must be deterministic across runs for reproducibility.
     -> same JSON always renders byte-identical prompt text.
  4. Context-dependent rules must be stated, not implied, or the model
     over-flags. -> context_required is injected as its own section
     (the OLD builder dropped this; that was a correctness bug).

v2 schema keys (policies_v2/<platform>/<category>.json):
  policy_name, policy_rationale, prohibited[], allowed[],
  context_required[], examples_violating[], examples_allowed[],
  enforcement
"""

import json
from pathlib import Path

# Point at the v2 policies. Change this if you move the folder.
POLICY_DIR = Path(__file__).parent.parent / "policies_v2"
PROMPT_DIR = Path(__file__).parent.parent / "prompts"

MAX_BODY_CHARS = 2000  # Lemmy megathreads can be 10KB+; cap to protect context window.


def _numbered(items: list[str]) -> str:
    """
    Render a list as numbered lines.

    Palla et al.: enumerated rules >> prose. The number is not decorative;
    it gives the model a discrete, countable unit to reason over and to
    cite back in its reasoning field.
    """
    if not items:
        return "  (none specified)"
    return "\n".join(f"  {i}. {line}" for i, line in enumerate(items, 1))


def load_policy(platform: str, category: str) -> dict:
    path = POLICY_DIR / platform / f"{category}.json"
    if not path.exists():
        raise FileNotFoundError(f"No policy JSON at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_template(has_image: bool) -> str:
    name = "template_vision.txt" if has_image else "template_text.txt"
    with open(PROMPT_DIR / name, "r", encoding="utf-8") as f:
        return f.read()


def build_prompt(
    platform: str,
    category: str,
    post_title: str,
    post_text: str,
    has_image: bool,
) -> tuple[str, bool]:
    """
    Build the final prompt for one post under one platform policy.

    platform: "meta" | "x"
    category: "violence" | "hate_speech" | "spam"

    Returns (prompt_string, was_truncated).
    """
    policy = load_policy(platform, category)
    template = load_template(has_image)

    truncated = False
    if len(post_text) > MAX_BODY_CHARS:
        post_text = post_text[:MAX_BODY_CHARS] + " [TRUNCATED]"
        truncated = True

    # context_required may be absent in some files; default to empty.
    context_required = policy.get("context_required", [])

    filled = template.format(
        platform_upper=platform.upper(),
        policy_name=policy["policy_name"],
        category=category,
        policy_rationale=policy["policy_rationale"],
        # v2 key names: prohibited / allowed (NOT rules_do_not_post / rules_allowed)
        rules_do_not_post=_numbered(policy["prohibited"]),
        rules_allowed=_numbered(policy["allowed"]),
        # NEW: context-dependent rules get their own section so the model
        # knows these are "needs more signal" cases, not auto-removals.
        rules_context_required=_numbered(context_required),
        examples_violating=_numbered(policy["examples_violating"]),
        examples_allowed=_numbered(policy["examples_allowed"]),
        post_title=post_title or "(none)",
        post_text=post_text,
        has_image="yes" if has_image else "no",
    )

    return filled, truncated


if __name__ == "__main__":
    p, trunc = build_prompt(
        platform="meta",
        category="violence",
        post_title="test",
        post_text="I am going to find you and end you.",
        has_image=False,
    )
    print(p)
    print("\n--- truncated:", trunc)
