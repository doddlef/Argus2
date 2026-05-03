# Agent Core Design

Argus is supposed to review code and react to PR thread comment just like a senior engineer.
At current stage, it should be aware of `PR opened` (when a Pull Request is opened), `PR synchronize` (when use push in a PR), and `issue comment` (when user make comment in the PR). In future, it may also react to resolve, emoji reacting.

I have built a demo version, which became a mess gradually, so I decide to refactor, and apply my experience in the design. In this blog, I will introduce my design, plan, and concern.

## Ports

The core module define the interface/protocol of ports, which should be implemented and provided by upper level, the github connector layer.
These ports is used to interact with the Github, or other version control in future. This will be provided in SessionContext/ApplicationContext, and used as `ctx.ports.reader`.

In my past design, I built `Reader` (Read-only access to repository content and PR discussion, has functions including `fetch_pr_metadata`, `read_file`, `list`, ...), and `Commenter` (Write access to PR comments and reviews, has functions including `post_review`, `post_comment`, ...).

## Dispatcher

`Dispatcher` is the entry point of Argus. Connect layer receive Github event, translate it to a Trigger, and inject needed data, like the ports. The connect layer then pass the trigger to the core dispatcher, who will decide which pipeline to use and execute.

> Question: In the demo project, I found the concern about multiple events at same time. For example, what should happen when user make a comment, or push again when Argus is doing the analysis? I want it to behave as an agent like the Claude Code, and considering block it until last event is consumed?

## Memory and Wiki

The memory has not be actually implemented. By in my idea, it will be in a wiki file system format, no RAG, and no embedding. The memory will stored under `/{configured_path}/argus/{project}/`.

Each project will have a `index.md`, which is the entry point of the memory system, it contains `name (path)`, `description` and type fields for files under it.
This will use as an index, and always loaded into conversation.

There will be tools `memory_read`, `memory_write`, and agent could call it to write the memory to its own file, like `future_plan.md`, ...

Another special file is `structure.md`, which contains a description of codebase structures and critical files/packages, for example, `docs/` for documents, `module.auth` for authentication, ...

I am considering another folder `branch`, which contains memory specific for one branch, we will copy or integral when branch or merge.

There will be a `pr` folder, which contains Pull Request conversation summary, and it also contains a last comment id in formatter, indicates when summary happened. When a PR contains too many messages, we will compact it before load into compact. Only summary and messages after last comment id will be loaded.

## Context

There are three layers of context:

1. `State`: the input and output of nodes/edges, just like the State in langGraph. This is node+edge dominated, `Any` type in llm_framework.
2. `SessionContext`: the context of each session, contain session info, usually immutable, for example, PR metadata (id, title), repo, commit SHA, ...
3. `ApplicationContext`: the global context, contain global data, like configuration, llm clients, ...

### Session Context

The `Dispatcher` should be responsible for constructing the `SessionContext`. Besides, the entry node of each pipeline should be no input required.

> Question: How should I maintain the `SessionContext`? and should it be readonly? In my past demo, I was using a ContextVar, just like the ThreadLocal in Java, but will this causes concurrency issue. Besides, should I use a `async with`?

For example,

```py
 _current_session: ContextVar[SessionContext] = ContextVar("session")

@asynccontextmanager
async def session(ctx: SessionContext):                                                                                                        
    token = _current_session.set(ctx)  
    try:
        yield ctx
    finally:           
        _current_session.reset(token)

async with session(SessionContext(pr_number=42, repo="org/repo", ...)):                                                                                           
    await workflow.run(event) 
```

### Application Context and Client Tires

Application context contains the global parameters, for example global configurations, clients.

I am considering using Factory Pattern and pre-defined tier clients. At startup, Argus read the configuration, and load the clients for different tier, `fast`, `standard`, and `deep`. For example, in a simple route node, it could use `app_ctx.clients.fast`, while in analysis node, it should use `app_ctx.clients.deep`.
RetryClient wrapping is automatic — create() wraps every client. Nodes never interact with an unwrapped client.

### Configurations

The configuration will work in three layers. First, it try to get env `ARGUS_CONFIG_PATH` or using a default value. This should be pointed to a `toml` file. For parameters not in toml, or toml not presented, it will read env first, then using default value, or throw an error.

> Question: should I worry about using Client as singleton will cause concurrency issue? I am using ollama and anthropic in client implementation, I think it should be concurrency safe? the only concern should be the mock client, but it is testing only.

## Memory

## PipeLine

Pipeline is a pre-defined workflow in core layer, it should has session context (side effect, local thread).

This is core business logic. As mentioned before, it should be aware of "opened", "syn", and "commented" at current stage.

I will introduce my previous implementation, and my issue later.

### PR open and syn

#### First version

In my previous PR, I was using the following workflow at first:

```txt
Data Load -> Triage: list[Plan] -> N * Analysis: Report -join-> Summary: Response -> Memory  
```

In the Triage node, it analysis PR metadata, commit history (SHA, message), file changes (only files list, status), memory (no history messages yet), and than generate a list of plan (files to read, guidance prompt, analysis level).

In the analysis node, it run a ReAct based on the metadata, ..., guidance prompt, and provided tools to read code file, diff, memory, then generate a report.

The Summary report collect reports, and make a comment based on it.

Memory node update the wiki based on input.

This comes up with serval issue, first, the summary response is very slow, as it will wait until all report is finished. Second, it seems costly, as we have 3 + (N * React) LLM calls, I am worry about it. Last, the memory updating performance bad.

#### Second version

Later, I switch to:

```txt
Data Load -> Triage: list[Plan] -> N * Analysis: Report -> Comment -join-> Summary: Response -> Memory 
```

This solve the "cold startup", but it does introduce more LLM call.

### Comment

I was using the following workflow:

```txt
Data Load -> Route -analysis-> Triage: list[Plan] -> N * Analysis: Report -join-> Summary: Response -> Memory  
                   -conversation-> Conversation: Response
```

### Idea

I gradually feel should we align more responsibility to the Agent itself? for example, let it self manage memory when analysis. Or we let the Memory node immediately update after analysis by reading the thinking/conversation of analysis.

Besides, I feel we have too many read/write tools. We have `pr_read_file`, `pr_read_diff`, `list`, `memory_read`, `memory_write`, ..., and I am worrying about reading file will use too much token, and take up the context window.