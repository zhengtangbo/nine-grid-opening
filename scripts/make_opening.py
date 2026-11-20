"""Select source clips and build a Jianying draft in one command."""

import argparse
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", type=Path, nargs="+")
    parser.add_argument("--name", required=True, help="new Jianying draft name")
    parser.add_argument("--drafts-root", type=Path)
    parser.add_argument("--closing-input", type=int, help="1-based index of the closing source")
    audio = parser.add_mutually_exclusive_group()
    audio.add_argument("--audio", type=Path, help="replace the bundled audio")
    audio.add_argument("--no-audio", action="store_true")
    args = parser.parse_args()
    scripts = Path(__file__).resolve().parent
    with TemporaryDirectory(prefix="nine-grid-plan-") as folder:
        plan = Path(folder) / "plan.json"
        select = [sys.executable, str(scripts / "select_highlights.py"),
                  "--out", str(plan), "--", *(str(path.resolve()) for path in args.videos)]
        result = subprocess.run(select)
        if result.returncode:
            return result.returncode
        create = [sys.executable, str(scripts / "create_jianying_draft.py"), str(plan), "--name", args.name]
        for option, value in (("--drafts-root", args.drafts_root),
                              ("--closing-input", args.closing_input), ("--audio", args.audio)):
            if value is not None:
                create.extend((option, str(value)))
        if args.no_audio:
            create.append("--no-audio")
        return subprocess.run(create).returncode


if __name__ == "__main__":
    sys.exit(main())
