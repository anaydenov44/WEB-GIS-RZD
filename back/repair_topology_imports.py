from pathlib import Path
import re

path = Path("scripts/build_route_scope_topology.py")
text = path.read_text(encoding="utf-8")

# Убираем ошибочно вставленные import-строки, если они попали внутрь from ... import (...)
bad_lines = {
    "import math",
    "from collections import defaultdict",
    "from typing import Any",
}

lines = text.splitlines()
cleaned: list[str] = []
paren_import_depth = 0

for line in lines:
    stripped = line.strip()

    if paren_import_depth > 0 and stripped in bad_lines:
        # пропускаем импорт, который оказался внутри многострочного import-блока
        continue

    cleaned.append(line)

    # очень простой трекер многострочных import-блоков
    if paren_import_depth == 0:
        if re.match(r"^\s*from\s+\S+\s+import\s*\(", line):
            paren_import_depth = line.count("(") - line.count(")")
    else:
        paren_import_depth += line.count("(") - line.count(")")
        if paren_import_depth <= 0:
            paren_import_depth = 0

text = "\n".join(cleaned) + "\n"

# Убираем дубли этих импортов на верхнем уровне, если они есть
for bad in bad_lines:
    text = re.sub(rf"^{re.escape(bad)}\n", "", text, flags=re.MULTILINE)

# Вставляем безопасный блок импортов сразу после обычных import ... строк в начале файла
lines = text.splitlines()
insert_at = 0

# пропускаем shebang / encoding / пустые строки в самом начале
while insert_at < len(lines) and (
    lines[insert_at].startswith("#!")
    or "coding" in lines[insert_at]
    or lines[insert_at].strip() == ""
):
    insert_at += 1

# идём по начальному import-блоку, корректно пропуская многострочные from ... import (...)
i = insert_at
last_import_line = insert_at - 1
while i < len(lines):
    stripped = lines[i].strip()

    if stripped.startswith("import ") or stripped.startswith("from "):
        last_import_line = i

        if stripped.startswith("from ") and "(" in stripped and ")" not in stripped:
            depth = lines[i].count("(") - lines[i].count(")")
            i += 1
            while i < len(lines) and depth > 0:
                depth += lines[i].count("(") - lines[i].count(")")
                last_import_line = i
                i += 1
            continue

        i += 1
        continue

    if stripped == "":
        i += 1
        continue

    break

safe_imports = [
    "import math",
    "from collections import defaultdict",
    "from typing import Any",
]

for import_line in reversed(safe_imports):
    lines.insert(last_import_line + 1, import_line)

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("OK: imports repaired")