"""Optional audit scanner for agent instruction-bearing context.

Detection provides telemetry for obvious prompt-injection patterns. A clean
scan is **not** a security guarantee and must not replace sandbox isolation,
credential separation, restricted egress, or output validation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

InstructionAuditMode = Literal["off", "audit"]

_DEFAULT_MAX_FILES = 50
_DEFAULT_MAX_FILE_BYTES = 256_000
_DEFAULT_MAX_TOTAL_BYTES = 1_000_000
_SNIPPET_LIMIT = 160


class FindingSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class InstructionAuditFinding:
    """Bounded, redacted finding for a suspicious instruction pattern."""

    pattern_id: str
    severity: FindingSeverity
    source: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return {
            "pattern_id": self.pattern_id,
            "severity": str(self.severity),
            "source": self.source,
            "snippet": self.snippet,
        }


@dataclass
class InstructionAuditReport:
    """Result of scanning instruction-bearing context for a container run."""

    mode: InstructionAuditMode
    findings: list[InstructionAuditFinding] = field(default_factory=list)
    files_scanned: int = 0
    bytes_scanned: int = 0
    truncated: bool = False
    error: str | None = None
    ticket_key: str | None = None
    repository: str | None = None
    workflow_stage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "findings": [f.to_dict() for f in self.findings],
            "files_scanned": self.files_scanned,
            "bytes_scanned": self.bytes_scanned,
            "truncated": self.truncated,
            "error": self.error,
            "ticket_key": self.ticket_key,
            "repository": self.repository,
            "workflow_stage": self.workflow_stage,
            "finding_count": len(self.findings),
        }


_PATTERNS: tuple[tuple[str, FindingSeverity, re.Pattern[str]], ...] = (
    (
        "ignore_previous_instructions",
        FindingSeverity.HIGH,
        re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I),
    ),
    (
        "system_prompt_override",
        FindingSeverity.HIGH,
        re.compile(r"(override|disregard)\s+(the\s+)?system\s+prompt", re.I),
    ),
    (
        "reveal_system_prompt",
        FindingSeverity.MEDIUM,
        re.compile(r"(reveal|show|print|dump)\s+(your\s+)?(system\s+)?prompt", re.I),
    ),
    (
        "exfiltrate_secrets",
        FindingSeverity.HIGH,
        re.compile(
            r"(exfiltrate|steal|leak|upload)\s+(all\s+)?(secrets?|credentials?|api\s*keys?|tokens?)",
            re.I,
        ),
    ),
    (
        "developer_mode_jailbreak",
        FindingSeverity.MEDIUM,
        re.compile(r"\b(DAN|developer\s+mode|jailbreak)\b", re.I),
    ),
)


def _redact_snippet(text: str, start: int, end: int) -> str:
    snippet = text[max(0, start - 40) : min(len(text), end + 40)]
    snippet = re.sub(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*\S+", r"\1=[REDACTED]", snippet)
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if len(snippet) > _SNIPPET_LIMIT:
        snippet = snippet[: _SNIPPET_LIMIT - 3] + "..."
    return snippet


def scan_text(
    text: str,
    *,
    source: str,
) -> list[InstructionAuditFinding]:
    """Scan a single text blob for known prompt-injection patterns."""
    findings: list[InstructionAuditFinding] = []
    if not text:
        return findings
    for pattern_id, severity, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                InstructionAuditFinding(
                    pattern_id=pattern_id,
                    severity=severity,
                    source=source,
                    snippet=_redact_snippet(text, match.start(), match.end()),
                )
            )
    return findings


def scan_instruction_context(
    *,
    paths: list[Path] | None = None,
    inline_texts: list[tuple[str, str]] | None = None,
    mode: InstructionAuditMode = "audit",
    max_files: int = _DEFAULT_MAX_FILES,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    ticket_key: str | None = None,
    repository: str | None = None,
    workflow_stage: str | None = None,
) -> InstructionAuditReport:
    """Scan selected instruction files and inline context.

    Independent of Podman/Kubernetes/OpenShell. ``mode="off"`` returns an empty
    report without reading files. Audit mode never alters workflow routing.
    """
    report = InstructionAuditReport(
        mode=mode,
        ticket_key=ticket_key,
        repository=repository,
        workflow_stage=workflow_stage,
    )
    if mode == "off":
        return report

    try:
        total = 0
        for source, text in inline_texts or []:
            encoded = text.encode("utf-8", errors="replace")
            if total + len(encoded) > max_total_bytes:
                report.truncated = True
                break
            total += len(encoded)
            report.bytes_scanned = total
            report.findings.extend(scan_text(text, source=source))

        for path in paths or []:
            if report.files_scanned >= max_files or total >= max_total_bytes:
                report.truncated = True
                break
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size > max_file_bytes:
                report.truncated = True
                continue
            if total + size > max_total_bytes:
                report.truncated = True
                break
            data = path.read_bytes()[:max_file_bytes]
            total += len(data)
            report.files_scanned += 1
            report.bytes_scanned = total
            text = data.decode("utf-8", errors="replace")
            report.findings.extend(scan_text(text, source=str(path)))
    except Exception as exc:  # noqa: BLE001 — audit must never break execution
        report.error = f"{type(exc).__name__}: scanner error"
        logger.warning("Instruction audit scanner error: %s", exc)

    if report.findings:
        logger.warning(
            "Instruction audit found %s pattern(s) for ticket=%s stage=%s "
            "(scan success is not a security guarantee)",
            len(report.findings),
            ticket_key,
            workflow_stage,
        )
    else:
        logger.info(
            "Instruction audit completed with no findings for ticket=%s stage=%s "
            "(not a security guarantee)",
            ticket_key,
            workflow_stage,
        )
    return report
