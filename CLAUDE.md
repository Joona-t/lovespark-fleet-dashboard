# Fleet Dashboard: rules for agents

Read `README.md` for the data flow. Run `python3 -m unittest test_fleet` before every commit.

| Rule | Enforced by |
|---|---|
| Agent output is untrusted. It enters only through `Fleet.ingest`, which rebuilds it in `clean()`. A new agent field goes into the schema sets next to `clean()`, or it arrives as `None`. | `test_no_shape_of_agent_output_can_break_the_stream`, `test_hostile_agent_output_cannot_reach_the_page_as_markup` |
| The server answers only an IP address, `localhost` or this machine's own names in `Host`. | `test_host_header_allowlist_blocks_dns_rebinding` |
| The Windows start command reads its script with a deadline, never a bare `ReadToEnd()`. | `test_windows_agent_start_gives_up_on_a_silent_session` |
| `agent_win.ps1` stays ASCII. | `test_windows_agent_is_ascii` |
| No machine names, addresses or keys in the repo: they belong in `devices.json`, which is ignored. | review |
