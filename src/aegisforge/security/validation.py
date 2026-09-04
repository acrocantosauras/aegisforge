"""Security validation for AegisForge Phase 3.

Validates: document security, model output, tenant isolation,
prompt injection detection, and input sanitization.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SecurityValidationResult:
    """Result of a security validation check."""

    passed: bool = True
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_violation(self, message: str) -> None:
        self.violations.append(message)
        self.passed = False

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)


# --- Document Security ---


def validate_document_upload(
    filename: str,
    content_type: str,
    content_size: int,
    max_size: int = 10_485_760,
    allowed_types: set[str] | None = None,
) -> SecurityValidationResult:
    """Validate a document upload for security."""
    result = SecurityValidationResult()

    # File size check
    if content_size > max_size:
        result.add_violation(f"Document exceeds maximum size of {max_size} bytes")

    if content_size == 0:
        result.add_violation("Document is empty")

    # Content type validation
    if allowed_types and content_type not in allowed_types:
        result.add_violation(f"Content type '{content_type}' is not allowed")

    # Safe filename
    if filename:
        # Check for path traversal
        if ".." in filename or "/" in filename or "\\" in filename:
            result.add_violation("Filename contains path traversal characters")

        # Check for executable extensions
        dangerous_extensions = {".exe", ".bat", ".cmd", ".sh", ".ps1", ".js", ".vbs", ".wsf"}
        ext = os.path.splitext(filename)[1].lower()
        if ext in dangerous_extensions:
            result.add_violation(f"File extension '{ext}' is potentially dangerous")

        # Check for null bytes
        if "\x00" in filename:
            result.add_violation("Filename contains null bytes")

    return result


def sanitize_filename(filename: str) -> str:
    """Sanitize a filename for safe storage."""
    # Remove path components
    filename = os.path.basename(filename)
    # Remove null bytes
    filename = filename.replace("\x00", "")
    # Replace dangerous characters
    filename = re.sub(r"[^\w\-.]", "_", filename)
    # Limit length
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        filename = name[:255 - len(ext)] + ext
    return filename or "unnamed"


# --- Tenant Isolation ---


def validate_tenant_access(
    requesting_org_id: str,
    resource_org_id: str,
    resource_type: str = "resource",
) -> SecurityValidationResult:
    """Validate that a request has proper tenant access."""
    result = SecurityValidationResult()

    if not requesting_org_id:
        result.add_violation("No organization ID provided for tenant check")

    if not resource_org_id:
        result.add_warning(f"{resource_type} has no organization ID (may be shared)")

    if requesting_org_id and resource_org_id and requesting_org_id != resource_org_id:
        result.add_violation(
            f"Cross-tenant access denied: {requesting_org_id} cannot access {resource_type} "
            f"owned by {resource_org_id}"
        )

    return result


# --- Prompt Injection Detection ---


INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"you\s+are\s+now\s+(a|an)\s+",
    r"disregard\s+(all\s+)?prior",
    r"new\s+instructions?:",
    r"system\s*prompt\s*:",
    r"<\|system\|>",
    r"\[INST\]",
    r"###\s*(system|human|assistant)\s*:",
    r"act\s+as\s+if\s+you",
    r"pretend\s+you\s+are",
    r"forget\s+everything",
    r"override\s+your\s+instructions",
]


def detect_prompt_injection(text: str) -> SecurityValidationResult:
    """Detect potential prompt injection in text content.

    Treats retrieved documents as untrusted data.
    """
    result = SecurityValidationResult()

    if not text:
        return result

    text_lower = text.lower()

    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text_lower):
            result.add_violation(
                f"Potential prompt injection detected: pattern '{pattern}' found in content"
            )

    # Check for unusually long content that might be injection attempts
    if len(text) > 50000:
        result.add_warning("Content is unusually long and may contain hidden instructions")

    return result


# --- Model Output Validation ---


def validate_model_output(
    output: str,
    expected_format: str = "text",
    max_length: int = 50000,
) -> SecurityValidationResult:
    """Validate LLM output for safety."""
    result = SecurityValidationResult()

    if not output:
        result.add_violation("Model output is empty")
        return result

    if len(output) > max_length:
        result.add_violation(f"Model output exceeds maximum length of {max_length}")

    # Check for injection patterns in output
    injection_check = detect_prompt_injection(output)
    if not injection_check.passed:
        result.violations.extend(injection_check.violations)
        result.passed = False

    # Validate JSON format if expected
    if expected_format == "json":
        import json
        try:
            json.loads(output)
        except json.JSONDecodeError:
            result.add_violation("Model output is not valid JSON")

    return result


# --- Secret Detection ---


SECRET_PATTERNS = [
    (r"api[_-]?key\s*[:=]\s*\S+", "API key"),
    (r"password\s*[:=]\s*\S+", "password"),
    (r"secret[_-]?key\s*[:=]\s*\S+", "secret key"),
    (r"token\s*[:=]\s*[A-Za-z0-9\-._]+", "token"),
    (r"Bearer\s+[A-Za-z0-9\-._]+", "bearer token"),
]


def detect_secrets(text: str) -> SecurityValidationResult:
    """Detect potential secrets in text."""
    result = SecurityValidationResult()

    if not text:
        return result

    for pattern, label in SECRET_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            result.add_warning(f"Potential {label} detected in content")

    return result


# --- Content Type Validation ---


ALLOWED_CONTENT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/json",
}


def validate_content_type(
    content_type: str,
    allowed: set[str] | None = None,
) -> SecurityValidationResult:
    """Validate a content type against allowed types."""
    result = SecurityValidationResult()
    allowed = allowed or ALLOWED_CONTENT_TYPES

    if content_type not in allowed:
        result.add_violation(
            f"Content type '{content_type}' is not in the allowed list: {sorted(allowed)}"
        )

    return result
