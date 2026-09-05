import re


class InvalidPatch(ValueError):
    pass


def extract_patch(text):
    if len(text.encode("utf-8")) > 256 * 1024:
        raise InvalidPatch("Patch response exceeds 256 KiB.")
    text = text.strip()
    if not text:
        return ""

    blocks = re.findall(
        r"```(?:diff|patch)?[ \t]*\r?\n(.*?)```",
        text,
        flags=re.DOTALL,
    )
    if blocks:
        if len(blocks) != 1:
            raise InvalidPatch("Return exactly one patch block.")
        text = blocks[0].strip()

    starts = [
        value for value in (
            text.find("diff --git "),
            text.find("--- a/"),
        ) if value >= 0
    ]
    if not starts:
        raise InvalidPatch("No unified diff found.")
    return text[min(starts):].rstrip() + "\n"


def validate_patch(patch, allowed_files=("calculator.py",)):
    if not patch:
        return

    forbidden = (
        "GIT binary patch", "Binary files ", "new file mode",
        "deleted file mode", "old mode ", "new mode ",
        "rename from ", "rename to ", "copy from ", "copy to ",
        "Subproject commit ",
    )
    lines = patch.splitlines()

    if any(line.startswith(forbidden) for line in lines):
        raise InvalidPatch("Binary, mode, rename, and file lifecycle changes are forbidden.")

    old_headers = 0
    new_headers = 0

    for line in lines:
        if line.startswith("diff --git "):
            valid = {
                f"diff --git a/{name} b/{name}" for name in allowed_files
            }
            if line not in valid:
                raise InvalidPatch("Patch changes a forbidden path.")

        if line.startswith("--- "):
            old_headers += 1
            if line[4:] not in {f"a/{name}" for name in allowed_files}:
                raise InvalidPatch("Invalid old-file path.")

        if line.startswith("+++ "):
            new_headers += 1
            if line[4:] not in {f"b/{name}" for name in allowed_files}:
                raise InvalidPatch("Invalid new-file path.")

    if old_headers == 0 or old_headers != new_headers:
        raise InvalidPatch("Missing or mismatched unified diff headers.")

    if not any(line.startswith("@@ ") for line in lines):
        raise InvalidPatch("Patch does not contain a hunk.")
