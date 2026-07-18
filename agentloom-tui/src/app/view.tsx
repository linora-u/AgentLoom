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

import {
  RGBA,
  TextAttributes,
  type InputRenderable,
  type ScrollBoxRenderable,
  type TextareaRenderable,
} from "@opentui/core"
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
import { isProblemRuntimeStatus } from "../domain"
import {
  buildPaletteItems,
  buildModelPaletteItems,
  buildSidebarGroups,
  flattenAgentCatalog,
  recentRunEntries,
  type PaletteItem,
  type SidebarEntry,
  type SidebarRunEntry,
} from "./controller"
import {
  runDetailSections,
  systemDetailSections,
  workspaceEntityDetail,
  type DetailSection,
} from "./presentation"
import {
  type AgentLoomSession,
  type AgentLoomSessionState,
  type BuilderMessage,
} from "./session"
import {
  AGENTLOOM_LOGO,
  SIDEBAR_WIDTH,
  createDefaultScrollAcceleration,
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

// Braille sequence and cadence follow OpenCode's spinner primitive at the
// pinned MIT-licensed upstream commit documented above.
const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"] as const

export function AgentLoomApp(props: AgentLoomAppProps) {
  const renderer = useRenderer()
  const dimensions = useTerminalDimensions()
  const mode = createMemo<ThemeMode>(() => renderer.themeMode === "light" ? "light" : "dark")
  const theme = createMemo(() => themeFor(mode()))
  const [state, setState] = createSignal(props.session.state)
  const [focus, setFocus] = createSignal<"builder" | "context">("builder")
  const [animationFrame, setAnimationFrame] = createSignal(0)
  const [paletteOpen, setPaletteOpen] = createSignal(false)
  const [paletteQuery, setPaletteQuery] = createSignal("")
  const [paletteSelected, setPaletteSelected] = createSignal(0)
  const [paletteScope, setPaletteScope] = createSignal<"all" | "models">("all")
  let input: TextareaRenderable | undefined
  let paletteInput: InputRenderable | undefined
  let contextScrollbox: ScrollBoxRenderable | undefined

  const unsubscribe = props.session.subscribe(setState)
  onCleanup(unsubscribe)
  createEffect(() => {
    const active = state().workspacePhase === "loading"
      || state().assistantBusy
      || state().detailBusy
    if (!active) {
      setAnimationFrame(0)
      return
    }
    const animation = setInterval(
      () => setAnimationFrame((value) => (value + 1) % SPINNER_FRAMES.length),
      80,
    )
    onCleanup(() => clearInterval(animation))
  })

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
  const snapshot = createMemo(() => state().snapshot)
  const groups = createMemo(() => buildSidebarGroups(snapshot()))
  const paletteItems = createMemo(() => paletteScope() === "models"
    ? buildModelPaletteItems(snapshot(), state().modelType)
    : buildPaletteItems(snapshot()))
  const filteredPaletteItems = createMemo(() => filterPaletteItems(paletteItems(), paletteQuery()))
  onMount(() => {
    setTimeout(() => {
      if (input?.isDestroyed) return
      input?.focus()
    }, 1)
  })

  createEffect(() => {
    if (!input || input.isDestroyed) return
    if (
      !paletteOpen()
      && focus() === "builder"
      && state().route.type === "builder"
      && !state().assistantBusy
    ) {
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
    if (event.ctrl && event.name === "p") {
      event.preventDefault()
      if (paletteOpen()) closePalette()
      else openPalette()
      return
    }
    if (paletteOpen()) {
      if (event.name === "escape") {
        event.preventDefault()
        closePalette()
        return
      }
      if (event.name === "up") {
        event.preventDefault()
        movePalette(-1)
        return
      }
      if (event.name === "down") {
        event.preventDefault()
        movePalette(1)
        return
      }
      if (event.name === "return" || event.name === "enter") {
        event.preventDefault()
        activatePaletteItem()
        return
      }
      return
    }
    if (event.name === "f6" && layout().visible) {
      event.preventDefault()
      if (focus() === "context") {
        setFocus("builder")
        setTimeout(() => input?.focus(), 1)
      } else {
        setFocus("context")
        input?.blur()
      }
      return
    }
    if (event.name === "tab") {
      event.preventDefault()
      openPalette()
      return
    }

    if (
      state().route.type === "run"
      && event.name === "a"
      && !event.ctrl
      && !event.meta
      && !event.option
      && !event.shift
    ) {
      event.preventDefault()
      setFocus("builder")
      void props.session.analyzeCurrentRun()
      setTimeout(() => input?.focus(), 1)
      return
    }

    if (
      state().route.type !== "builder"
      && (event.name === "escape" || (event.name === "b" && !event.ctrl && !event.meta && !event.option))
    ) {
      event.preventDefault()
      props.session.goBuilder()
      if (layout().mode === "overlay") props.session.setSidebarOpen(false)
      setFocus("builder")
      return
    }

    const contextNavigation = ["pageup", "pagedown"].includes(event.name)
      || (focus() === "context" && ["up", "down", "home", "end"].includes(event.name))
    if (layout().visible && contextNavigation) {
      navigateContext(event)
      return
    }

    const editing = renderer.currentFocusedEditor === input
    if (editing) return

    if (
      focus() === "builder"
      && !state().assistantBusy
      && !event.shift
      && !event.ctrl
      && !event.meta
      && !event.option
      && !event.super
      && !event.hyper
      && ["return", "enter", "kpenter", "linefeed"].includes(event.name)
    ) {
      event.preventDefault()
      input?.submit()
      return
    }

    if (event.name === "q") {
      event.preventDefault()
      props.onExit()
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

  function openPalette(scope: "all" | "models" = "all") {
    setPaletteScope(scope)
    setPaletteQuery("")
    setPaletteSelected(0)
    setPaletteOpen(true)
    input?.blur()
    setTimeout(() => paletteInput?.focus(), 1)
  }

  function closePalette(restoreBuilderFocus = true) {
    setPaletteOpen(false)
    paletteInput?.blur()
    if (restoreBuilderFocus) {
      setFocus("builder")
      setTimeout(() => input?.focus(), 1)
    } else {
      setFocus("context")
      input?.blur()
    }
  }

  function movePalette(delta: number) {
    const count = filteredPaletteItems().length
    if (count === 0) return
    setPaletteSelected((current) => (current + delta + count) % count)
  }

  function navigateContext(event: Parameters<ScrollBoxRenderable["handleKeyPress"]>[0]) {
    if (!layout().visible || !contextScrollbox || contextScrollbox.isDestroyed) return
    event.preventDefault()
    contextScrollbox.handleKeyPress(event)
  }

  function activatePaletteItem(index = paletteSelected()) {
    const item = filteredPaletteItems()[index]
    if (!item) return
    if ("modelType" in item) {
      closePalette()
      void props.session.submit(`/model ${item.modelType}`)
      return
    }
    if ("entry" in item) {
      closePalette(false)
      void props.session.openEntry(item.entry)
      return
    }
    if (item.action === "models") {
      openPalette("models")
      return
    }
    closePalette()
    if (item.action === "chat") props.session.goBuilder()
    if (item.action === "refresh") void props.session.refresh()
    if (item.action === "apply") void props.session.submit("/apply")
    if (item.action === "schedules") void props.session.submit("/schedule")
  }

  function submitBuilder(value: string) {
    if (value.trim() === "/models") {
      openPalette("models")
      return
    }
    void props.session.submit(value)
  }

  function openContextEntry(entry: SidebarEntry) {
    setFocus("context")
    input?.blur()
    void props.session.openEntry(entry)
  }

  function closeContext() {
    props.session.goBuilder()
    if (layout().mode === "overlay") props.session.setSidebarOpen(false)
    setFocus("builder")
  }

  function analyzeCurrentRun() {
    setFocus("builder")
    void props.session.analyzeCurrentRun()
    setTimeout(() => input?.focus(), 1)
  }

  return (
    <box width="100%" height="100%" backgroundColor={theme().background} flexDirection="row">
      <box flexGrow={1} minWidth={0} height="100%">
        <Header
          theme={theme()}
          project={state().snapshot.project.name || props.projectRoot}
          model={state().modelType}
          workspacePhase={state().workspacePhase}
          assistantBusy={state().assistantBusy}
          spinner={SPINNER_FRAMES[animationFrame()]!}
        />
        <box flexGrow={1} minHeight={0} paddingLeft={2} paddingRight={2} paddingBottom={1}>
          <BuilderView
            state={state()}
            theme={theme()}
            focus={focus() === "builder" && !paletteOpen()}
            bindInput={(value) => (input = value)}
            submit={submitBuilder}
            navigateContext={navigateContext}
            focusContext={() => {
              setFocus("context")
              input?.blur()
            }}
            spinner={SPINNER_FRAMES[animationFrame()]!}
          />
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
            <ContextSidebar
              state={state()}
              groups={groups()}
              theme={theme()}
              mode={mode()}
              spinner={SPINNER_FRAMES[animationFrame()]!}
              bindScrollbox={(value) => (contextScrollbox = value)}
              onOpenEntry={openContextEntry}
              onAnalyzeRun={analyzeCurrentRun}
              onClose={closeContext}
              onFocus={() => {
                setFocus("context")
                input?.blur()
              }}
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
              <ContextSidebar
                state={state()}
                groups={groups()}
                theme={theme()}
                mode={mode()}
                spinner={SPINNER_FRAMES[animationFrame()]!}
                bindScrollbox={(value) => (contextScrollbox = value)}
                onOpenEntry={openContextEntry}
                onAnalyzeRun={analyzeCurrentRun}
                onClose={closeContext}
                onFocus={() => {
                  setFocus("context")
                  input?.blur()
                }}
              />
            </box>
          </Match>
        </Switch>
      </Show>
      <Show when={paletteOpen()}>
        <CommandPalette
          items={filteredPaletteItems()}
          selected={paletteSelected()}
          query={paletteQuery()}
          scope={paletteScope()}
          theme={theme()}
          bindInput={(value) => (paletteInput = value)}
          onQuery={(value) => {
            setPaletteQuery(value)
            setPaletteSelected(0)
          }}
          onHover={setPaletteSelected}
          onSelect={activatePaletteItem}
          onClose={closePalette}
        />
      </Show>
    </box>
  )
}

function Header(props: {
  theme: AgentLoomPalette
  project: string
  model: string | null
  workspacePhase: AgentLoomSessionState["workspacePhase"]
  assistantBusy: boolean
  spinner: string
}) {
  const activity = () => {
    if (props.workspacePhase === "loading") return `${props.spinner} 正在索引项目…`
    if (props.workspacePhase === "error") return "索引失败 · /refresh 重试"
    if (props.assistantBusy) return `${props.spinner} AgentLoom 正在思考…`
    return `model: ${props.model ?? "未配置"}`
  }
  return (
    <box flexShrink={0} paddingLeft={2} paddingRight={2} paddingTop={1} paddingBottom={1}>
      <For each={AGENTLOOM_LOGO}>
        {(line) => <text fg={props.theme.primary} wrapMode="none">{line}</text>}
      </For>
      <box flexDirection="row" justifyContent="space-between" marginTop={1}>
        <text fg={props.theme.text} attributes={TextAttributes.BOLD}>{props.project}</text>
        <text fg={props.workspacePhase === "error" ? props.theme.error : props.workspacePhase === "loading" || props.assistantBusy ? props.theme.warning : props.theme.muted}>
          {activity()}
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
  navigateContext: (event: Parameters<ScrollBoxRenderable["handleKeyPress"]>[0]) => void
  focusContext: () => void
  spinner: string
}) {
  let textarea: TextareaRenderable | undefined
  const modelSummary = () => props.state.snapshot.models.items
    .filter((item) => item.configured)
    .map((item) => `${item.type}${item.default ? "*" : ""}`)
    .join(" · ") || "未配置"

  function submit() {
    const value = textarea?.plainText.trim() ?? ""
    if (!value || props.state.assistantBusy) return
    textarea?.clear()
    props.submit(value)
  }

  function requestSubmit() {
    // Match OpenCode's IME handling: Return can arrive before the final CJK
    // composition is flushed into plainText, so read the buffer two tasks later.
    setTimeout(() => setTimeout(submit, 0), 0)
  }

  return (
    <box flexGrow={1} minHeight={0}>
      <box flexDirection="row" justifyContent="space-between" flexShrink={0} marginBottom={1}>
        <text fg={props.theme.text} attributes={TextAttributes.BOLD}>AgentLoom Chat</text>
        <text fg={props.theme.muted}>普通对话 · 项目观察 · Agent YAML 提案</text>
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
          <Show when={props.state.streamingText}>
            <box flexShrink={0} border={["left"]} borderColor={props.theme.secondary} paddingLeft={1} paddingTop={1} paddingBottom={1}>
              <text fg={props.theme.secondary} attributes={TextAttributes.BOLD}>AgentLoom · streaming</text>
              <text fg={props.theme.text} wrapMode="word">{props.state.streamingText}</text>
            </box>
          </Show>
          <For each={props.state.activities}>
            {(activity) => (
              <text fg={activity.state === "completed" ? props.theme.success : props.theme.warning}>
                {activity.state === "completed" ? "✓" : props.spinner} {activity.name}
              </text>
            )}
          </For>
          <Show when={props.state.assistantBusy}>
            <box flexShrink={0} border={["left"]} borderColor={props.theme.warning} paddingLeft={1} paddingTop={1} paddingBottom={1}>
              <text fg={props.theme.warning}>{props.spinner} AgentLoom 正在思考并调用所需能力…</text>
            </box>
          </Show>
          <Show when={props.state.draft?.files.length ? props.state.draft : undefined}>
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
          focused={props.focus && !props.state.assistantBusy}
          minHeight={1}
          maxHeight={5}
          placeholder="问任何问题，或让我查看/创建 Agent；输入 /models、/refresh、/apply"
          placeholderColor={props.theme.muted}
          textColor={props.theme.text}
          focusedTextColor={props.theme.text}
          backgroundColor={props.theme.element}
          focusedBackgroundColor={props.theme.element}
          cursorColor={props.state.assistantBusy ? props.theme.element : props.theme.primary}
          keyBindings={[
            { name: "return", action: "submit" },
            { name: "kpenter", action: "submit" },
            { name: "linefeed", action: "submit" },
            { name: "return", shift: true, action: "newline" },
            { name: "kpenter", shift: true, action: "newline" },
          ]}
          onKeyDown={(event) => {
            if (event.name === "f6") {
              event.preventDefault()
              props.focusContext()
              return
            }
            if (["pageup", "pagedown"].includes(event.name)) {
              props.navigateContext(event)
              return
            }
            if (props.state.assistantBusy) event.preventDefault()
          }}
          onSubmit={requestSubmit}
          onMouseDown={() => textarea?.focus()}
        />
      </box>
      <box flexShrink={0} flexDirection="row" gap={2} paddingTop={1}>
        <text
          id="agentloom-send-control"
          fg={props.state.assistantBusy ? props.theme.muted : props.theme.primary}
          attributes={props.state.assistantBusy ? undefined : TextAttributes.BOLD}
          onMouseDown={requestSubmit}
        >
          [ Enter 发送 ]
        </text>
        <text fg={props.theme.muted}>/apply 显式写入</text>
        <text fg={props.theme.muted}>Ctrl+P 浏览工作台</text>
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
  spinner: string
  theme: AgentLoomPalette
  bindScrollbox?: (value: ScrollBoxRenderable) => void
}) {
  return (
    <box flexGrow={1} minHeight={0}>
      <box flexShrink={0} marginBottom={1}>
        <text fg={props.theme.text} attributes={TextAttributes.BOLD}>{props.title}</text>
        <text fg={props.theme.muted}>{props.subtitle}</text>
      </box>
      <Show when={props.sections.length > 0} fallback={<text fg={props.busy ? props.theme.warning : props.theme.muted}>{props.busy ? `${props.spinner} 正在读取详情…` : "没有详情"}</text>}>
        <scrollbox
          id="agentloom-context-detail-scrollbox"
          ref={(value: ScrollBoxRenderable) => props.bindScrollbox?.(value)}
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

function ContextSidebar(props: {
  state: AgentLoomSessionState
  groups: ReturnType<typeof buildSidebarGroups>
  theme: AgentLoomPalette
  mode: ThemeMode
  spinner: string
  bindScrollbox: (value: ScrollBoxRenderable) => void
  onOpenEntry: (entry: SidebarEntry) => void
  onAnalyzeRun: () => void
  onClose: () => void
  onFocus: () => void
}) {
  const catalogDetail = createMemo(() => workspaceEntityDetail(props.state.snapshot, props.state.route))
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
      onMouseDown={props.onFocus}
    >
      <Switch>
        <Match when={props.state.route.type === "system"}>
          <DetailView
            title="Agent"
            subtitle={props.state.systemDetail?.summary.application_id ?? "正在读取"}
            sections={props.state.systemDetail ? systemDetailSections(props.state.systemDetail) : []}
            busy={props.state.detailBusy}
            spinner={props.spinner}
            theme={props.theme}
            bindScrollbox={props.bindScrollbox}
          />
        </Match>
        <Match when={props.state.route.type === "run"}>
          <DetailView
            title="Run"
            subtitle={props.state.runDetail?.summary.run_id ?? "正在读取"}
            sections={props.state.runDetail ? runDetailSections(props.state.runDetail) : []}
            busy={props.state.detailBusy}
            spinner={props.spinner}
            theme={props.theme}
            bindScrollbox={props.bindScrollbox}
          />
        </Match>
        <Match when={catalogDetail()}>
          {(detail) => (
            <DetailView
              title={detail().title}
              subtitle={detail().subtitle}
              sections={detail().sections}
              busy={false}
              spinner={props.spinner}
              theme={props.theme}
              bindScrollbox={props.bindScrollbox}
            />
          )}
        </Match>
        <Match when={props.state.route.type === "builder"}>
          <WorkspaceOverview
            state={props.state}
            groups={props.groups}
            theme={props.theme}
            mode={props.mode}
            bindScrollbox={props.bindScrollbox}
            onOpenEntry={props.onOpenEntry}
          />
        </Match>
      </Switch>
      <box flexShrink={0} paddingTop={1} gap={0}>
        <Show when={
          props.state.route.type === "run"
          && props.state.runDetail
          && isProblemRuntimeStatus(props.state.runDetail.summary.status)
        }>
          <text
            fg={props.theme.primary}
            attributes={TextAttributes.BOLD}
            onMouseDown={props.onAnalyzeRun}
          >
            [ a AI 分析原因 ]
          </text>
        </Show>
        <Show when={props.state.route.type !== "builder"}>
          <text fg={props.theme.secondary} onMouseDown={props.onClose}>Esc / b 返回工作区概览</text>
        </Show>
        <text fg={props.theme.muted}>F6 切换面板 · Ctrl+P 搜索所有实体与命令</text>
      </box>
    </box>
  )
}

function WorkspaceOverview(props: {
  state: AgentLoomSessionState
  groups: ReturnType<typeof buildSidebarGroups>
  theme: AgentLoomPalette
  mode: ThemeMode
  bindScrollbox: (value: ScrollBoxRenderable) => void
  onOpenEntry: (entry: SidebarEntry) => void
}) {
  const active = () => props.groups.runs.filter((entry) => entry.status === "running")
  const count = (status: SidebarRunEntry["status"]) => (
    props.groups.runs.filter((entry) => entry.status === status).length
  )
  const recent = () => recentRunEntries(props.groups.runs)
  const visibleSkills = () => props.groups.skills.slice(0, 5)
  const agentCount = () => {
    const catalogCount = flattenAgentCatalog(props.state.snapshot).length
    return catalogCount || props.groups.systems.length
  }
  return (
    <scrollbox
      id="agentloom-workspace-scrollbox"
      ref={(value: ScrollBoxRenderable) => props.bindScrollbox(value)}
      flexGrow={1}
      minHeight={0}
      scrollAcceleration={createDefaultScrollAcceleration()}
      verticalScrollbarOptions={{
        trackOptions: {
          backgroundColor: props.theme.panel,
          foregroundColor: props.theme.borderActive,
        },
      }}
    >
      <box gap={1} paddingRight={1}>
        <box flexShrink={0}>
          <text fg={props.theme.text} attributes={TextAttributes.BOLD}>项目总览</text>
          <text fg={props.theme.muted}>{props.state.snapshot.project.name}</text>
        </box>
        <text fg={props.theme.primary} attributes={TextAttributes.BOLD}>定义</text>
        <box flexShrink={0} border={["left"]} borderColor={props.theme.primary} paddingLeft={1}>
          <text fg={props.theme.text}>
            {props.state.snapshot.applications.length} Applications · {agentCount()} Agents
          </text>
          <text fg={props.theme.text}>
            已发现 {props.state.snapshot.skills.length} 个 Skill · {props.state.snapshot.schedules.items.length} 个定时任务
          </text>
        </box>
        <text fg={props.theme.primary} attributes={TextAttributes.BOLD}>运行记录</text>
        <box flexShrink={0} border={["left"]} borderColor={props.theme.primary} paddingLeft={1}>
          <text fg={props.theme.text}>
            {props.groups.runs.length} 次 · {count("completed")} 成功 · {count("failed")} 失败
          </text>
          <text fg={props.theme.text}>
            {count("crashed")} 崩溃 · {count("interrupted")} 中断 · {active().length} 运行中
          </text>
          <Show when={count("unknown") > 0}>
            <text fg={props.theme.warning}>{count("unknown")} 状态未知</text>
          </Show>
          <text fg={props.state.snapshot.schedules.service.state === "error" ? props.theme.error : props.theme.muted}>
            调度服务: {schedulerStateLabel(props.state.snapshot.schedules.service.state)}
          </text>
        </box>
        <Show when={props.state.snapshot.worker_invocations_incomplete}>
          <box flexShrink={0} border={["left"]} borderColor={props.theme.warning} paddingLeft={1}>
            <text fg={props.theme.warning}>Worker 状态索引不完整</text>
            <text fg={props.theme.muted}>部分历史调用无法确认；不会把它们误报为 Never run。</text>
          </box>
        </Show>
        <Show when={props.state.runsIncomplete}>
          <box flexShrink={0} border={["left"]} borderColor={props.theme.warning} paddingLeft={1}>
            <text fg={props.theme.warning}>Run 状态索引正在收敛</text>
            <text fg={props.theme.muted}>当前是有界增量窗口；后续刷新会继续对账新增与删除。</text>
          </box>
        </Show>
        <Show when={visibleSkills().length > 0}>
          <text fg={props.theme.primary} attributes={TextAttributes.BOLD}>
            Skills ({props.groups.skills.length})
          </text>
          <For each={visibleSkills()}>
            {(entry) => (
              <box flexShrink={0} paddingBottom={1} onMouseDown={() => props.onOpenEntry(entry)}>
                <text fg={props.theme.text} wrapMode="none" truncate>{entry.title}</text>
                <text fg={props.theme.muted} wrapMode="none" truncate>{entry.subtitle} · 点击查看</text>
              </box>
            )}
          </For>
          <Show when={props.groups.skills.length > visibleSkills().length}>
            <text fg={props.theme.muted}>
              还有 {props.groups.skills.length - visibleSkills().length} 个 · Ctrl+P 查看全部
            </text>
          </Show>
        </Show>
        <text fg={props.theme.primary} attributes={TextAttributes.BOLD}>最近执行</text>
        <Show when={recent().length > 0} fallback={<text fg={props.theme.muted}>暂无运行记录</text>}>
          <For each={recent()}>
            {(entry) => {
              const presentation = () => statusPresentation(entry.status)
            return (
              <box
                flexShrink={0}
                paddingBottom={1}
                onMouseDown={() => props.onOpenEntry(entry)}
              >
                <box flexDirection="row" gap={1}>
                  <text fg={statusColor(entry.status, props.mode)}>{presentation().symbol}</text>
                  <text fg={props.theme.text} wrapMode="none" truncate>{entry.title}</text>
                </box>
                <text fg={props.theme.muted} wrapMode="none" truncate>
                  {workspaceRunStatusLabel(entry.status)} · {entry.startedAt ?? "时间未知"} · 点击查看
                </text>
              </box>
            )
            }}
          </For>
        </Show>
      </box>
    </scrollbox>
  )
}

function schedulerStateLabel(state: AgentLoomSessionState["snapshot"]["schedules"]["service"]["state"]): string {
  return {
    running: "运行中",
    stopped: "未启动",
    stale: "状态过期",
    error: "异常",
  }[state]
}

function workspaceRunStatusLabel(status: SidebarRunEntry["status"]): string {
  return {
    running: "运行中",
    completed: "成功",
    interrupted: "已中断",
    failed: "失败",
    crashed: "崩溃",
    unknown: "未知",
  }[status]
}

function CommandPalette(props: {
  items: PaletteItem[]
  selected: number
  query: string
  scope: "all" | "models"
  theme: AgentLoomPalette
  bindInput: (value: InputRenderable) => void
  onQuery: (value: string) => void
  onHover: (index: number) => void
  onSelect: (index: number) => void
  onClose: () => void
}) {
  const dimensions = useTerminalDimensions()
  let scrollbox: ScrollBoxRenderable | undefined
  createEffect(() => {
    if (!scrollbox || props.selected < 0) return
    scrollbox.scrollChildIntoView(`agentloom-palette-entry-${props.selected}`)
  })
  return (
    <box
      position="absolute"
      top={0}
      left={0}
      right={0}
      bottom={0}
      zIndex={3000}
      alignItems="center"
      paddingTop={Math.max(1, Math.floor(dimensions().height / 6))}
      backgroundColor={RGBA.fromInts(0, 0, 0, 150)}
      onMouseDown={props.onClose}
    >
      <box
        width={Math.min(88, Math.max(36, dimensions().width - 4))}
        maxHeight={Math.max(12, dimensions().height - 6)}
        paddingTop={1}
        paddingBottom={1}
        paddingLeft={2}
        paddingRight={2}
        backgroundColor={props.theme.panel}
        border
        borderColor={props.theme.borderActive}
        onMouseDown={(event: { stopPropagation(): void }) => event.stopPropagation()}
      >
        <box flexDirection="row" justifyContent="space-between" flexShrink={0}>
          <text fg={props.theme.text} attributes={TextAttributes.BOLD}>
            {props.scope === "models"
              ? "模型选择 · config/llm.yaml"
              : "Commands · Models · Applications · Agents · Skills · Schedules · Runs"}
          </text>
          <text fg={props.theme.muted}>esc</text>
        </box>
        <box flexShrink={0} marginTop={1} marginBottom={1} border={["bottom"]} borderColor={props.theme.border}>
          <input
            ref={(value: InputRenderable) => props.bindInput(value)}
            value={props.query}
            onInput={props.onQuery}
            placeholder={props.scope === "models"
              ? "搜索并选择模型"
              : "搜索 Model、Application、Agent、Skill、Schedule、Run 或命令"}
            placeholderColor={props.theme.muted}
            textColor={props.theme.text}
            focusedTextColor={props.theme.text}
            backgroundColor={props.theme.panel}
            focusedBackgroundColor={props.theme.panel}
            cursorColor={props.theme.primary}
          />
        </box>
        <Show when={props.items.length > 0} fallback={<text fg={props.theme.muted}>没有匹配项</text>}>
          <scrollbox
            id="agentloom-palette-scrollbox"
            ref={(value: ScrollBoxRenderable) => (scrollbox = value)}
            flexGrow={1}
            minHeight={0}
            maxHeight={Math.max(6, dimensions().height - 14)}
            scrollAcceleration={createDefaultScrollAcceleration()}
            verticalScrollbarOptions={{
              trackOptions: {
                backgroundColor: props.theme.panel,
                foregroundColor: props.theme.borderActive,
              },
            }}
          >
            <box paddingRight={1}>
              <For each={props.items}>
                {(item, index) => {
                  const selected = () => props.selected === index()
                  return (
                    <box
                      id={`agentloom-palette-entry-${index()}`}
                      flexShrink={0}
                      paddingLeft={1}
                      paddingRight={1}
                      backgroundColor={selected() ? props.theme.element : props.theme.panel}
                      onMouseOver={() => props.onHover(index())}
                      onMouseDown={() => props.onSelect(index())}
                    >
                      <box flexDirection="row" justifyContent="space-between">
                        <text fg={selected() ? props.theme.text : props.theme.muted} wrapMode="none" truncate>{item.title}</text>
                        <text fg={props.theme.primary}>{item.category}</text>
                      </box>
                      <text fg={props.theme.muted} wrapMode="none" truncate>{item.description}</text>
                    </box>
                  )
                }}
              </For>
            </box>
          </scrollbox>
        </Show>
        <text flexShrink={0} marginTop={1} fg={props.theme.muted}>↑↓ 选择 · Enter 打开 · 输入筛选</text>
      </box>
    </box>
  )
}

function filterPaletteItems(items: PaletteItem[], rawQuery: string): PaletteItem[] {
  const query = rawQuery.trim().toLowerCase()
  if (!query) return items.slice(0, 200)
  const tokens = query.split(/\s+/).filter(Boolean)
  return items
    .map((item, index) => {
      const title = item.title.toLowerCase()
      const haystack = `${title} ${item.description.toLowerCase()} ${item.category.toLowerCase()}`
      if (!tokens.every((token) => haystack.includes(token))) return null
      const score = title === query ? 0 : title.startsWith(query) ? 1 : title.includes(query) ? 2 : 3
      return { item, score, index }
    })
    .filter((value): value is { item: PaletteItem; score: number; index: number } => value !== null)
    .sort((left, right) => left.score - right.score || left.index - right.index)
    .slice(0, 200)
    .map((value) => value.item)
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
      <text fg={props.theme.muted}>AgentLoom · 全局运行状态自动刷新 · r 重新索引</text>
      <text fg={props.theme.muted}>Ctrl-C 退出</text>
    </box>
  )
}
