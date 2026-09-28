# Writing an adapter plug-in

ectype reads thirteen stores out of the box. For a fourteenth you do not need to change ectype:
a separate package can register an adapter, and ectype loads it at start.

This folder is a complete, working example: a "chat app" whose store is a folder of
`*.chat.jsonl` files. Try it:

```bash
pip install integrations/adapter-plugin        # or: pip install -e integrations/adapter-plugin
mkdir -p ~/.example-agent/chats
printf '%s\n' '{"role":"user","text":"hello","ts":"2026-01-02T03:04:05Z"}' \
              '{"role":"assistant","text":"hi","ts":"2026-01-02T03:04:06Z"}' > ~/.example-agent/chats/first.chat.jsonl
ectype agents                                  # "example" is listed, marked as a plug-in
ectype list -a example
ectype show first
```

What a plug-in consists of:

1. **A class that extends `ectype.adapters.base.Adapter`** with `name`, `label`, `env_home`,
   `default_home` and `category`, and the two methods `discover()` (cheap: one `SessionRef` per
   session, never parse whole transcripts) and `load()` (one `Session` in the canonical model:
   `Message`s of `ContentBlock`s, kinds `text`, `thinking`, `tool_call`, `tool_result`, `system`,
   `info`, `error`, `image`). `ectype_adapter_example.py` is the smallest one that does both.
2. **One entry point** in the package's `pyproject.toml`, group `ectype.adapters`:

   ```toml
   [project.entry-points."ectype.adapters"]
   example = "ectype_adapter_example:ExampleAdapter"
   ```

That is all. Optional methods with sensible defaults: `artifacts()` (which files make up a
session, for `backup` and `fixture`), `rename()` (only if the store has a real place for a title),
`workspaces()`, `find()`. And one optional attribute, `question_tools`: the names of the agent's
tools that put a question to the user and hand back the answer, so a view can show them as the
question and the answer they are.

Rules ectype applies to plug-ins: a plug-in that fails to import, is not an `Adapter`, or reuses
a name that is already taken is reported on stderr and skipped, never allowed to break the
listing. Store paths resolve the same way as for the built-ins: `$<env_home>` first, then the
path saved in Settings → Agents, then `default_home`. Chat-app plug-ins (`category = "chat"`)
are off in the web UI until switched on, like the built-in chat apps.
