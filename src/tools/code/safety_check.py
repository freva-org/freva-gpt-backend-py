import re
from dataclasses import dataclass
from typing import Optional, Tuple

from src.core.logging_setup import configure_logging

logger = configure_logging(__name__, named_log="code_server")

# Code safety rules
SAFETY_RULES = [
    (
        "exec_builtin",
        "Use of built-in exec() (arbitrary code execution).",
        re.compile(r"\bexec\s*\("),
    ),
    (
        "eval_builtin",
        "Use of built-in eval() (arbitrary code execution).",
        # Negative lookbehind prevents matching ".eval(" like model.eval(
        re.compile(r"(?<!\.)\beval\s*\("),
    ),
    (
        "compile_exec_eval",
        "Use of compile() which can be combined with exec/eval.",
        re.compile(r"\bcompile\s*\("),
    ),

    # --- Imports / dynamic import ---
    (
        "import_os",
        "Importing os can enable filesystem/process operations.",
        re.compile(r"^\s*(?:import\s+os\b|from\s+os\s+import\b)", re.MULTILINE),
    ),
    (
        "import_sys",
        "Importing sys can enable interpreter/process interactions.",
        re.compile(r"^\s*(?:import\s+sys\b|from\s+sys\s+import\b)", re.MULTILINE),
    ),
    (
        "dunder_import",
        "Use of __import__ (dynamic import).",
        re.compile(r"\b__import__\s*\("),
    ),
    (
        "importlib_import",
        "Use of importlib.import_module (dynamic import).",
        re.compile(r"\bimportlib\s*\.\s*import_module\s*\("),
    ),

    # --- Process / system ---
    (
        "os_system",
        "Use of os.system() (shell execution).",
        re.compile(r"\bos\s*\.\s*system\s*\("),
    ),
    (
        "subprocess",
        "Use of subprocess (process spawning).",
        re.compile(r"\bsubprocess\b"),
    ),

    # --- Network ---
    (
        "socket",
        "Use of socket (network access).",
        re.compile(r"\bsocket\b"),
    ),

    # --- Potentially risky modules ---
    (
        "ctypes",
        "Use of ctypes (FFI / memory-level operations).",
        re.compile(r"\bctypes\b"),
    ),
    (
        "pickle",
        "Use of pickle (unsafe deserialization if data is untrusted).",
        re.compile(r"\bpickle\b"),
    ),
    (
        "shutil",
        "Use of shutil (filesystem manipulation).",
        re.compile(r"\bshutil\b"),
    ),

    # --- Notebook escapes ---
    (
        "get_ipython",
        "Use of get_ipython (access to IPython internals).",
        re.compile(r"\bget_ipython\b"),
    ),
    (
        "jupyter_magic_or_shell",
        "Jupyter magic (%) or shell escape (!) line detected.",
        re.compile(r"(?m)^\s*[!%]"),
    ),
]


@dataclass(frozen=True)
class SafetyViolation:
    rule_id: str
    description: str
    match: str


def check_code_safety(code: str) -> Tuple[bool, Optional[SafetyViolation]]:
    """
    Returns (is_safe, violation). If not safe, violation explains why.
    """
    for rule_id, desc, rx in SAFETY_RULES:
        m = rx.search(code)
        if m:
            # include a small snippet of what matched to show the user
            matched = m.group(0)
            return False, SafetyViolation(rule_id=rule_id, description=desc, match=matched)
    return True, None
