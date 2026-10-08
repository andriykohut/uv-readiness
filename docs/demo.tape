# Records docs/demo.gif. Run from the repository root:
#
#     vhs docs/demo.tape
#
# The hidden setup creates a small project locked as of 2024-03-01, before any
# package had a Python 3.13 wheel, and points `uvx uv-readiness` at this checkout.

Output docs/demo.gif

Require uv

Set Shell bash
Set FontSize 15
Set Width 960
Set Height 800
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 60ms

Hide
Type `REPO=$PWD && mkdir -p "$(mktemp -d)/shop" && cd "$_"` Enter
Type `printf '[project]\nname = "shop"\nversion = "1.0"\nrequires-python = ">=3.10"\ndependencies = ["scipy<1.12", "pandas", "pyyaml", "requests"]\n' > pyproject.toml` Enter
Type `uv lock -q --exclude-newer 2024-03-01` Enter
Type `uvx() { uv run -q --project "$REPO" "$@"; }` Enter
Type `PS1='shop $ ' && clear` Enter
Sleep 6s
Show

Type "uvx uv-readiness 3.13" Sleep 800ms Enter
Sleep 13s
