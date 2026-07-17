/**
 * Renderer structure and responsive right-sidebar behavior are derived from:
 * - OpenCode packages/tui/src/app.tsx
 * - OpenCode packages/tui/src/routes/session/index.tsx
 * - OpenCode packages/tui/src/routes/session/sidebar.tsx
 *
 * Upstream commit: efb6cc2d4bf6332eb156709795d2b3a649198b65
 * License: MIT; see ../../upstream/LICENSE.opencode.
 * AgentLoom removes OpenCode's coding/session/tool surface and retains the
 * terminal renderer, conversation center, and responsive detail directory.
 */

import { RGBA, TextAttributes, type TextareaRenderable } from "@opentui/core"
import { useKeyboard, useRenderer, useTerminalDimensions } from "@opentui/solid"
import {
  For,
  Match,
  Show,
  Switch,
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  onMount,
} from "solid-js"
import { buildSidebarGroups, type SidebarEntry } from "./controller"
import { runDetailSections, systemDetailSections, type DetailSection } from "./presentation"
import {
  type AgentLoomSession,
  type AgentLoomSessionState,
  type BuilderMessage,
} from "./session"
import {
  AGENTLOOM_LOGO,
  SIDEBAR_WIDTH,
  resolveSidebarLayout,
  statusColor,
  statusPresentation,
  themeFor,
  type AgentLoomPalette,
  type ThemeMode,
} from "../ui"

export type AgentLoomAppProps = {
  session: AgentLoomSession
  projectRoot: string
  onExit: () => void
  refreshIntervalMs?: number
}

export function AgentLoomApp(props: AgentLoomAppProps) {
  const renderer = useRenderer()
  const dimensions = useTerminalDimensions()
  const mode = createMemo<ThemeMode>(() => renderer.themeMode === "light" ? "light" : "dark")
  const theme = createMemo(() => themeFor(mode()))
  const [state, setState] = createSignal(props.session.state)
  const [focus, setFocus] = createSignal<"builder" | "sidebar">("builder")
  let input: TextareaRenderable | undefined

  const unsubscribe = props.session.subscribe(setState)
  onCleanup(unsubscribe)

  const refreshEvery = props.refreshIntervalMs ?? 2_000
  if (refreshEvery > 0) {
    const interval = setInterval(() => void props.session.refreshLive(), refreshEvery)
    onCleanup(() => clearInterval(interval))
  }

  const layout = createMemo(() => resolveSidebarLayout({
    terminalWidth: dimensions().width,
    preference: "auto",
    open: state().sidebarOpen,
  }))
  const groups = createMemo(() => {
    state()
    return buildSidebarGroups(props.session.state.snapshot)
  })
  onMount(() => {
    setTimeout(() => {
      if (input?.isDestroyed) return
      input?.focus()
    }, 1)
  })

  createEffect(() => {
    if (!input || input.isDestroyed) return
    if (focus() === "builder" && state().route.type === "builder" && !state().busy) {
      input.focus()
      return
    }
    input.blur()
  })

  useKeyboard((event) => {
    if (event.ctrl && event.name === "c") {
      event.preventDefault()
      props.onExit()
      return
    }
    if (event.name === "tab") {
      event.preventDefault()
      if (focus() === "builder") {
        setFocus("sidebar")
        props.session.setSidebarOpen(true)
      } else {
        setFocus("builder")
        if (layout().mode === "overlay") props.session.setSidebarOpen(false)
      }
      return
    }

    const editing = renderer.currentFocusedEditor === input
    if (editing) return

    if (event.name === "q") {
      event.preventDefault()
      props.onExit()
      return
    }
    if (event.name === "up") {
      event.preventDefault()
      props.session.select(-1)
      return
    }
    if (event.name === "down") {
      event.preventDefault()
      props.session.select(1)
      return
    }
    if (event.name === "return" || event.name === "enter") {
      event.preventDefault()
      void props.session.openSelected()
      return
    }
    if (event.name === "r") {
      event.preventDefault()
      void props.session.refresh()
      return
    }
    if (event.name === "b") {
      event.preventDefault()
      props.session.goBuilder()
      setFocus("builder")
      return
    }
    if (event.name === "s") {
      event.preventDefault()
      props.session.setSidebarOpen(!state().sidebarOpen)
      return
    }
    if (event.name === "escape") {
      event.preventDefault()
      if (layout().mode === "overlay" && state().sidebarOpen) {
        props.session.setSidebarOpen(false)
      } else {
        props.session.goBuilder()
        setFocus("builder")
      }
    }
  })

  return (
    <box width="100%" height="100%" backgroundColor={theme().background} flexDirection="row">
      <box flexGrow={1} minWidth={0} height="100%">
        <Header
          theme={theme()}
          project={state().snapshot.project.name || props.projectRoot}
          model={state().modelType}
          busy={state().busy}
        />
        <box flexGrow={1} minHeight={0} paddingLeft={2} paddingRight={2} paddingBottom={1}>
          <Switch>
            <Match when={state().route.type === "builder"}>
              <BuilderView
                state={state()}
                theme={theme()}
                focus={focus() === "builder"}
                bindInput={(value) => (input = value)}
                submit={(value) => void props.session.submit(value)}
              />
            </Match>
            <Match when={state().route.type === "system"}>
              <DetailView
                title="Agent System"
                subtitle={state().systemDetail?.summary.application_id ?? "加载中…"}
                sections={state().systemDetail ? systemDetailSections(state().systemDetail!) : []}
                busy={state().busy}
                theme={theme()}
              />
            </Match>
            <Match when={state().route.type === "run"}>
              <DetailView
                title="Agent Run"
                subtitle={state().runDetail?.summary.run_id ?? "加载中…"}
                sections={state().runDetail ? runDetailSections(state().runDetail!) : []}
                busy={state().busy}
                theme={theme()}
              />
            </Match>
          </Switch>
          <Show when={state().notice}>
            <box flexShrink={0} marginTop={1} paddingLeft={1} border={["left"]} borderColor={theme().warning}>
              <text fg={theme().warning} wrapMode="word">{state().notice}</text>
            </box>
          </Show>
        </box>
        <Footer theme={theme()} />
      </box>

      <Show when={layout().visible}>
        <Switch>
          <Match when={layout().mode === "inline"}>
            <Sidebar
              groups={groups()}
              selected={state().selectedIndex}
              theme={theme()}
              mode={mode()}
              onSelect={(entry, index) => {
                props.session.setSelected(index)
                void props.session.openEntry(entry)
              }}
              onHover={(index) => props.session.setSelected(index)}
            />
          </Match>
          <Match when={layout().mode === "overlay"}>
            <box
              position="absolute"
              top={0}
              left={0}
              right={0}
              bottom={0}
              zIndex={100}
              alignItems="flex-end"
              backgroundColor={RGBA.fromInts(0, 0, 0, 90)}
            >
              <Sidebar
                groups={groups()}
                selected={state().selectedIndex}
                theme={theme()}
                mode={mode()}
                onSelect={(entry, index) => {
                  props.session.setSelected(index)
                  void props.session.openEntry(entry)
                }}
                onHover={(index) => props.session.setSelected(index)}
              />
            </box>
          </Match>
        </Switch>
      </Show>
    </box>
  )
}

function Header(props: {
  theme: AgentLoomPalette
  project: string
  model: string | null
  busy: boolean
}) {
  return (
    <box flexShrink={0} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1}>
      <For each={AGENTLOOM_LOGO}>
        {(line) => <text fg={props.theme.primary} wrapMode="none">{line}</text>}
      </For>
      <box flexDirection="row" justifyContent="space-between" marginTop={1}>
        <text fg={props.theme.text} attributes={TextAttributes.BOLD}>{props.project}</text>
        <text fg={props.busy ? props.theme.warning : props.theme.muted}>
          {props.busy ? "处理中…" : `model: ${props.model ?? "未配置"}`}
        </text>
      </box>
    </box>
  )
}

function BuilderView(props: {
  state: AgentLoomSessionState
  theme: AgentLoomPalette
  focus: boolean
  bindInput: (input: TextareaRenderable) => void
  submit: (value: string) => void
}) {
  let textarea: TextareaRenderable | undefined
  const modelSummary = () => props.state.snapshot.models.items
    .filter((item) => item.configured)
    .map((item) => `${item.type}${item.default ? "*" : ""}`)
    .join(" · ") || "未配置"

  function submit() {
    const value = textarea?.plainText.trim() ?? ""
    if (!value || props.state.busy) return
    textarea?.clear()
    props.submit(value)
  }

  return (
    <box flexGrow={1} minHeight={0}>
      <box flexDirection="row" justifyContent="space-between" flexShrink={0} marginBottom={1}>
        <text fg={props.theme.text} attributes={TextAttributes.BOLD}>Agent Builder</text>
        <text fg={props.theme.muted}>只生成与校验 YAML，不执行 Agent</text>
      </box>
      <text flexShrink={0} fg={props.theme.muted} marginBottom={1}>
        {`Models: ${modelSummary()}`}
      </text>
      <scrollbox
        flexGrow={1}
        minHeight={0}
        stickyScroll
        stickyStart="bottom"
        verticalScrollbarOptions={{
          trackOptions: {
            backgroundColor: props.theme.background,
            foregroundColor: props.theme.borderActive,
          },
        }}
      >
        <box gap={1} paddingRight={1}>
          <For each={props.state.messages}>
            {(message) => <ChatMessageView message={message} theme={props.theme} />}
          </For>
          <Show when={props.state.draft}>
            {(draft) => (
              <box
                flexShrink={0}
                border={["left"]}
                borderColor={draft().valid ? props.theme.success : props.theme.error}
                paddingLeft={1}
                paddingTop={1}
                paddingBottom={1}
                gap={1}
              >
                <text fg={props.theme.text} attributes={TextAttributes.BOLD}>
                  Draft revision {draft().revision} · {draft().valid ? "校验通过" : "校验失败"}
                </text>
                <For each={draft().errors}>
                  {(error) => <text fg={props.theme.error} wrapMode="word">{error}</text>}
                </For>
                <For each={draft().files}>
                  {(file) => (
                    <box flexShrink={0} gap={1}>
                      <text fg={props.theme.secondary}>{file.change} {file.path}</text>
                      <For each={file.content.split("\n").slice(0, 24)}>
                        {(line) => <text fg={props.theme.muted} wrapMode="word">  {line || " "}</text>}
                      </For>
                      <Show when={file.content.split("\n").length > 24}>
                        <text fg={props.theme.muted}>  …</text>
                      </Show>
                    </box>
                  )}
                </For>
              </box>
            )}
          </Show>
        </box>
      </scrollbox>
      <box
        flexShrink={0}
        border
        borderColor={props.focus ? props.theme.borderActive : props.theme.border}
        paddingLeft={1}
        paddingRight={1}
        backgroundColor={props.theme.element}
      >
        <textarea
          ref={(value: TextareaRenderable) => {
            textarea = value
            props.bindInput(value)
          }}
          focused={props.focus && !props.state.busy}
          minHeight={1}
          maxHeight={5}
          placeholder="描述要创建的 Agent，或输入 /models、/model <type>、/refresh、/apply"
          placeholderColor={props.theme.muted}
          textColor={props.theme.text}
          focusedTextColor={props.theme.text}
          backgroundColor={props.theme.element}
          focusedBackgroundColor={props.theme.element}
          cursorColor={props.state.busy ? props.theme.element : props.theme.primary}
          onKeyDown={(event) => {
            if (props.state.busy) event.preventDefault()
          }}
          onSubmit={() => setTimeout(() => setTimeout(submit, 0), 0)}
          onMouseDown={() => textarea?.focus()}
        />
      </box>
      <box flexShrink={0} flexDirection="row" gap={2} paddingTop={1}>
        <text fg={props.theme.muted}>Enter 发送</text>
        <text fg={props.theme.muted}>/apply 显式写入</text>
        <text fg={props.theme.muted}>Tab 浏览状态</text>
      </box>
    </box>
  )
}

function ChatMessageView(props: { message: BuilderMessage; theme: AgentLoomPalette }) {
  const user = () => props.message.role === "user"
  return (
    <box
      flexShrink={0}
      border={["left"]}
      borderColor={user() ? props.theme.primary : props.theme.secondary}
      paddingLeft={1}
      paddingTop={1}
      paddingBottom={1}
    >
      <text fg={user() ? props.theme.primary : props.theme.secondary} attributes={TextAttributes.BOLD}>
        {user() ? "你" : "AgentLoom"}
      </text>
      <text fg={props.theme.text} wrapMode="word">{props.message.content}</text>
    </box>
  )
}

function DetailView(props: {
  title: string
  subtitle: string
  sections: DetailSection[]
  busy: boolean
  theme: AgentLoomPalette
}) {
  return (
    <box flexGrow={1} minHeight={0}>
      <box flexShrink={0} marginBottom={1}>
        <text fg={props.theme.text} attributes={TextAttributes.BOLD}>{props.title}</text>
        <text fg={props.theme.muted}>{props.subtitle}</text>
      </box>
      <Show when={props.sections.length > 0} fallback={<text fg={props.theme.muted}>{props.busy ? "加载中…" : "没有详情"}</text>}>
        <scrollbox
          flexGrow={1}
          minHeight={0}
          verticalScrollbarOptions={{
            trackOptions: {
              backgroundColor: props.theme.background,
              foregroundColor: props.theme.borderActive,
            },
          }}
        >
          <box gap={1} paddingRight={1}>
            <For each={props.sections}>
              {(section) => (
                <box flexShrink={0} gap={0}>
                  <text fg={props.theme.primary} attributes={TextAttributes.BOLD}>{section.title}</text>
                  <For each={section.lines}>
                    {(line) => <text fg={props.theme.text} wrapMode="word">{line}</text>}
                  </For>
                </box>
              )}
            </For>
          </box>
        </scrollbox>
      </Show>
    </box>
  )
}

function Sidebar(props: {
  groups: ReturnType<typeof buildSidebarGroups>
  selected: number
  theme: AgentLoomPalette
  mode: ThemeMode
  onSelect: (entry: SidebarEntry, index: number) => void
  onHover: (index: number) => void
}) {
  return (
    <box
      width={SIDEBAR_WIDTH}
      height="100%"
      flexShrink={0}
      paddingTop={1}
      paddingBottom={1}
      paddingLeft={2}
      paddingRight={2}
      backgroundColor={props.theme.panel}
      border={["left"]}
      borderColor={props.theme.border}
    >
      <scrollbox
        flexGrow={1}
        minHeight={0}
        verticalScrollbarOptions={{
          trackOptions: {
            backgroundColor: props.theme.panel,
            foregroundColor: props.theme.borderActive,
          },
        }}
      >
        <box gap={1} paddingRight={1}>
          <SidebarGroup
            title="Agent Systems"
            entries={props.groups.systems}
            offset={0}
            selected={props.selected}
            theme={props.theme}
            mode={props.mode}
            onSelect={props.onSelect}
            onHover={props.onHover}
          />
          <SidebarGroup
            title="Runs"
            entries={props.groups.runs}
            offset={props.groups.systems.length}
            selected={props.selected}
            theme={props.theme}
            mode={props.mode}
            onSelect={props.onSelect}
            onHover={props.onHover}
          />
        </box>
      </scrollbox>
      <box flexShrink={0} paddingTop={1}>
        <text fg={props.theme.muted}>↑↓ 选择 · Enter 查看 · b Builder</text>
        <text fg={props.theme.muted}>r 刷新 · q 退出</text>
      </box>
    </box>
  )
}

function SidebarGroup(props: {
  title: string
  entries: SidebarEntry[]
  offset: number
  selected: number
  theme: AgentLoomPalette
  mode: ThemeMode
  onSelect: (entry: SidebarEntry, index: number) => void
  onHover: (index: number) => void
}) {
  return (
    <box flexShrink={0} gap={0}>
      <text fg={props.theme.text} attributes={TextAttributes.BOLD}>
        {props.title} ({props.entries.length})
      </text>
      <Show when={props.entries.length > 0} fallback={<text fg={props.theme.muted}>  暂无</text>}>
        <For each={props.entries}>
          {(entry, localIndex) => {
            const index = () => props.offset + localIndex()
            const selected = () => props.selected === index()
            const presentation = () => statusPresentation(entry.status)
            return (
              <box
                flexShrink={0}
                paddingLeft={1}
                paddingRight={1}
                paddingTop={1}
                paddingBottom={1}
                backgroundColor={selected() ? props.theme.element : props.theme.panel}
                onMouseDown={() => props.onSelect(entry, index())}
                onMouseOver={() => props.onHover(index())}
              >
                <box flexDirection="row" gap={1}>
                  <text fg={statusColor(entry.status, props.mode)}>{presentation().symbol}</text>
                  <text fg={selected() ? props.theme.text : props.theme.muted} wrapMode="none" truncate>
                    {entry.title}
                  </text>
                </box>
                <text fg={props.theme.muted} wrapMode="none" truncate>{entry.subtitle}</text>
                <text fg={statusColor(entry.status, props.mode)}>{presentation().label}</text>
              </box>
            )
          }}
        </For>
      </Show>
    </box>
  )
}

function Footer(props: { theme: AgentLoomPalette }) {
  return (
    <box
      flexShrink={0}
      height={1}
      paddingLeft={2}
      paddingRight={2}
      flexDirection="row"
      justifyContent="space-between"
      backgroundColor={props.theme.panel}
    >
      <text fg={props.theme.muted}>AgentLoom · Run 每 2 秒刷新 · r 刷新目录</text>
      <text fg={props.theme.muted}>Ctrl-C 退出</text>
    </box>
  )
}
