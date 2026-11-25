---
name: draft-issue
description: Draft a GitHub issue for tadtruex/KicadTools, show it, and post it only after approval. Use when the user says "draft an enhancement" (label enhancement, type Feature) or "draft an issue" (label bug, type Bug).
---

# Drafting GitHub issues for KicadTools

## Which kind

| The user says          | Label         | Issue type |
|------------------------|---------------|------------|
| "draft an enhancement" | `enhancement` | `Feature`  |
| "draft an issue"       | `bug`         | `Bug`      |

Every issue is assigned to the user (`@me`).

## Steps

1. **Research the issue first.** Look at the code and the notes (`NOTES.md`, the `*_PLAN.md` files) so the body points to the real files, functions and routes. Say what exists today and what is missing. For a bug, add the steps to reproduce it and the expected and actual behaviour.
2. **Show the full draft**: title, label, type, assignee and body. Ask whether to submit it. **Do not post until the user says yes.** Make any changes they ask for and show the draft again.
3. **Create the issue:**
   ```
   gh issue create --title "<title>" --label <label> --assignee @me --body "<body>"
   ```
4. **Set the type.** gh 2.46 has no `--type` flag, so use the REST API with the number from the URL returned by step 3:
   ```
   gh api -X PATCH repos/tadtruex/KicadTools/issues/<n> -f type=Feature   # or Bug
   ```
5. Report the issue URL, and confirm its label, type and assignee.

## Setup on a new machine

gh needs to be logged in. Use a fine-grained personal access token limited to `tadtruex/KicadTools`, with **Issues: Read and write** (Metadata read-only is added automatically). Ask the user to save the token to a file and run:

```
! gh auth login --with-token < ~/token.txt && rm ~/token.txt
```

Don't ask them to paste the token into the chat. Check the login with `gh auth status`.

The repo's issue types are Task, Bug and Feature.
