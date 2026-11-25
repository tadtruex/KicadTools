---
name: draft-issue
description: Draft a GitHub issue for tadtruex/KicadTools, show it, and post it only after approval. Use when the user says "draft", "new", "file", "create" or "open" followed by "an enhancement" (label enhancement, type Feature), "an issue" (label bug, type Bug) or "a gui check" (label usability, type Task), e.g. "new issue", "new enhancement", "draft a gui check".
---

# Drafting GitHub issues for KicadTools

## Which kind

| The user says                  | Label         | Issue type |
|--------------------------------|---------------|------------|
| "draft / new enhancement"      | `enhancement` | `Feature`  |
| "draft / new issue"            | `bug`         | `Bug`      |
| "draft / new gui check"        | `usability`   | `Task`     |

"Draft", "new", "file", "create" and "open" all mean the same thing here: e.g. "new issue", "new enhancement", "file a gui check". Every one of them starts with a draft that the user approves, never a direct post.

Every issue is assigned to the user (`@me`).

## Steps

1. **Research the issue first.** Look at the code and the notes (`NOTES.md`, the `*_PLAN.md` files) so the body points to the real files, functions and routes. Say what exists today and what is missing. For a bug, add the steps to reproduce it and the expected and actual behaviour. For a gui check, list the pages or controls to try, on which devices (desktop browser, phone), and what to look for.
2. **Show the full draft**: title, label, type, assignee and body. Ask whether to submit it. **Do not post until the user says yes.** Make any changes they ask for and show the draft again.
3. **Create the issue:**
   ```
   gh issue create --title "<title>" --label <label> --assignee @me --body "<body>"
   ```
4. **Set the type.** gh 2.46 has no `--type` flag, so use the REST API with the number from the URL returned by step 3:
   ```
   gh api -X PATCH repos/tadtruex/KicadTools/issues/<n> -f type=Feature   # or Bug, Task
   ```
5. Report the issue URL, and confirm its label, type and assignee.

## Setup on a new machine

gh needs to be logged in. Use a fine-grained personal access token limited to `tadtruex/KicadTools`, with **Issues: Read and write** (Metadata read-only is added automatically). Ask the user to save the token to a file and run:

```
! gh auth login --with-token < ~/token.txt && rm ~/token.txt
```

Don't ask them to paste the token into the chat. Check the login with `gh auth status`.

The repo's issue types are Task, Bug and Feature.
