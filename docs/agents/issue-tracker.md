# Issue tracker

AgentLoom uses [GitHub Issues](https://github.com/linora-u/AgentLoom/issues) in
`linora-u/AgentLoom`. The Git remote is `origin`.

Read an issue and its discussion with:

```sh
gh issue view NUMBER --repo linora-u/AgentLoom --json number,title,body,comments,url
```

Specifications ready for implementation use the `ready-for-agent` label.
Architecture/Application definition work originates in issue #67; its checked-in
specification and task graph live under `docs/specs/`. Issues #17 and #19 remain
separate configuration-validation discussions and are not closed by this work.
