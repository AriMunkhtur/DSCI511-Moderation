"""
prompt_builder_v2.py

Takes a policy JSON + a post and builds a single structured prompt string
for the local LLM (Ollama).

Why v2: the original builder dumped policy text in as a big  block,
which Palla et al. (FAccT 2025, "Policy-as-Prompt") show is the wrong
move  LLMs are sensitive to how the policy is presented, not just
what it says. Three things from that paper drive this builder:

  - Structured/numbered rules. So _numbered() renders every rule as its own numbered line.
  - Prohibited and allowed need their own sections, not mixed together,
    or the model over removes.
  - Context dependent rules ("needs more signal") need their own
    section too. The old builder skipped this, which was a real bug.

v2 policy JSON schema (policies_v2/<platform>/<category>.json):
    policy_name, policy_rationale, prohibited[], allowed[],
    context_required[], examples_violating[], examples_allowed[],
    enforcement
"""

import json
from pathlib import Path


# point at the v2 policies (change if folder moves)
POLICY_DIR = Path(__file__).parent.parent / "policies_v2"
PROMPT_DIR = Path(__file__).parent.parent / "prompts"

# Lemmy megathreads can be 10KB+ so we cap to protect the context window
MAX_BODY_CHARS = 2000

  """Render a list as numbered lines for the prompt."""
def _numbered(items):
  
    if not items:
        return "  (none specified)"
    return "\n".join(f"  {i}. {line}" for i, line in enumerate(items, 1))

"""Load one policy JSON from policies_v2/<platform>/<category>.json."""
def load_policy(platform, category):
    
    path = POLICY_DIR / platform / f"{category}.json"
    if not path.exists():
        raise FileNotFoundError(f"No policy JSON at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_template(has_image):
    """Pick the right prompt template based on whether the post has an image."""
    name = "template_vision.txt" if has_image else "template_text.txt"
    with open(PROMPT_DIR / name, "r", encoding="utf-8") as f:
        return f.read()


def build_prompt(platform: str, category: str, post_title: str,
                 post_text: str, has_image: bool) -> tuple[str, bool]:
    """
    Build the prompt for one post under one platform's policy.

    platform: 'meta' or 'x'
    category: 'violence', 'hate_speech', or 'spam'
    Returns (prompt_string, was_truncated).
    """
    policy = load_policy(platform, category)
    template = load_template(has_image)

    truncated = False
    if len(post_text) > MAX_BODY_CHARS:
        post_text = post_text[:MAX_BODY_CHARS] + " [TRUNCATED]"
        truncated = True

    # context_required is optional in some policy files
    context_required = policy.get("context_required", [])

    filled = template.format(
        platform_upper=platform.upper(),
        policy_name=policy["policy_name"],
        category=category,
        policy_rationale=policy["policy_rationale"],
        # v2 key names: 'prohibited' and 'allowed' (not 'rules_do_not_post' etc)
        rules_do_not_post=_numbered(policy["prohibited"]),
        rules_allowed=_numbered(policy["allowed"]),
        rules_context_required=_numbered(context_required),
        examples_violating=_numbered(policy["examples_violating"]),
        examples_allowed=_numbered(policy["examples_allowed"]),
        post_title=post_title or "(none)",
        post_text=post_text,
        has_image="yes" if has_image else "no",
    )

    return filled, truncated

# quick sanity check
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
