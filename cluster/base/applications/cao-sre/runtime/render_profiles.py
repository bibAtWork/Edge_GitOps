"""Render the same SRE agent definition for each CAO harness."""

from pathlib import Path


CONFIG = Path("/config")
OUTPUT = Path("/home/cao/agent-profiles")
AGENTS = ("screener-triage", "screener-rca")
HARNESSES = ("opencode", "codex")


def compose(agent: str, harness: str, source: str, overlay: str) -> str:
    if not source.startswith("---\n"):
        raise ValueError(f"{agent}: missing YAML frontmatter")
    end = source.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"{agent}: unterminated YAML frontmatter")

    header = source[4:end].splitlines()
    if any(line.startswith(("provider:", "model:", "codexProfile:")) for line in header):
        raise ValueError(f"{agent}: harness settings belong in overlays")
    if harness == "codex":
        tool_index = next((i for i, line in enumerate(header) if line.startswith("allowedTools:")), None)
        if tool_index is not None:
            tool_end = tool_index + 1
            while tool_end < len(header) and header[tool_end].startswith((" ", "\t")):
                tool_end += 1
            del header[tool_index:tool_end]
        header = [
            f"name: {agent}-codex" if line.startswith("name:") else line
            for line in header
        ]

    return "---\n" + "\n".join(header) + "\n" + overlay.rstrip("\n") + "\n---\n" + source[end + 5 :]


def render(agent: str, harness: str, config: Path = CONFIG, output: Path = OUTPUT) -> Path:
    source = (config / f"{agent}.md").read_text(encoding="utf-8")
    overlay = (config / f"{harness}-overlay.yaml").read_text(encoding="utf-8")
    target = output / (f"{agent}-codex.md" if harness == "codex" else f"{agent}.md")
    target.write_text(compose(agent, harness, source, overlay), encoding="utf-8")
    return target


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for agent in AGENTS:
        for harness in HARNESSES:
            print(render(agent, harness))


if __name__ == "__main__":
    main()
