# Workflow staging folder

These are byte-identical copies of the three files in `.github/workflows/`.

They live here because GitHub's web uploader silently drops files inside
dotted folders, so `.github/` never survives a drag-and-drop — which forces
copy-pasting into the web editor, and pasting rendered text from a browser
introduces non-breaking spaces and smart quotes that break YAML. A single
U+00A0 in the indentation makes a workflow fail to parse, and GitHub then
shows its file path instead of its name.

## Getting them into place without pasting

`workflows/` has no leading dot, so it uploads normally. Then rename each file
into position, which moves the bytes exactly as they are:

1. Open `workflows/refresh.yml`
2. Click the pencil
3. In the filename box, replace the name with:
   `../.github/workflows/refresh.yml`
4. Commit

Repeat for `capture.yml` and `tests.yml`. Typing `../` moves up a directory
and typing `/` creates one, so GitHub relocates the file on commit.

If a file already exists at the destination, **delete it first**. Overwriting
by paste is what produced `The identifier 'deployment' may not be used more
than once within the same scope` — the new content landed on top of the old
instead of replacing it, leaving two deploy steps in one job.

## Verifying

In the Actions sidebar each workflow should appear as **refresh**, **capture**
and **tests**. If one shows its file path instead, GitHub could not parse it:
open it from the sidebar and the error names the line.
