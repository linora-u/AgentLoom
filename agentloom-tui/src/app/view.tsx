/**
 * Renderer structure and responsive right-sidebar behavior are derived from:
 * - OpenCode packages/tui/src/app.tsx
 * - OpenCode packages/tui/src/routes/session/index.tsx
 * - OpenCode packages/tui/src/routes/session/sidebar.tsx
 *
 * Upstream release: v1.18.3 (127bdb30784d508cc556c71a0f32b508a3061517)
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
  applicationHealthLabel,
  applicationRunStateLabel,
  buildPaletteItems,
  buildModelPaletteItems,
  buildSidebarGroups,
  recentRunEntries,
  type PaletteItem,
  type SidebarEntry,
  type SidebarRunEntry,
} from "./controller"
import {
  applicationDetailSections,
  effectiveAgentDetail,
  runDetailSections,
  studioToolOutput,
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
import { shouldReduceMotion } from "../ui/motion"

export type AgentLoomAppProps = {
  session: AgentLoomSession
  projectRoot: string
  onExit: () => void
  onRestart?: () => void
  refreshIntervalMs?: number
  reducedMotion?: boolean
}

type SelectionClipboardRenderer = Pick<
  ReturnType<typeof useRenderer>,
  "getSelection" | "copyToClipboardOSC52" | "clearSelection"
>

export function copySelectedText(renderer: SelectionClipboardRenderer): boolean {
  const text = renderer.getSelection()?.getSelectedText()
  if (!text) return false
  const copied = renderer.copyToClipboardOSC52(text)
  if (copied) renderer.clearSelection()
  return copied
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
  const reducedMotion = props.reducedMotion ?? shouldReduceMotion()
  const spinner = createMemo(() => reducedMotion ? "•" : SPINNER_FRAMES[animationFrame()]!)
  let input: TextareaRenderable | undefined
  let paletteInput: InputRenderable | undefined
  let chatScrollbox: ScrollBoxRenderable | undefined
  let contextScrollbox: ScrollBoxRenderable | undefined

  const unsubscribe = props.session.subscribe(setState)
  onCleanup(unsubscribe)
  createEffect(() => {
    const active = state().workspacePhase === "loading"
      || state().assistantBusy
      || state().detailBusy
      || !["idle", "waiting_permission", "failed"].includes(state().loopState)
    if (!active) {
      setAnimationFrame(0)
      return
    }
    if (reducedMotion) return
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
  const studioModelItems = createMemo<PaletteItem[]>(() => state().studioModels.map((model) => ({
    key: `studio-model:${model.id}`,
    category: "Models",
    title: model.name,
    description: [
      model.providerName,
      model.default ? "default" : "",
      state().studioModel?.id === model.id ? "selected" : "",
    ].filter(Boolean).join(" · "),
    modelType: model.id,
  })))
  const modelItems = createMemo(() => state().studioEnabled
    ? studioModelItems()
    : buildModelPaletteItems(snapshot(), state().modelType))
  const updateItems = createMemo<PaletteItem[]>(() => state().updatePhase === "available"
    ? [{
        key: "command:update",
        category: "Commands",
        title: "更新 AgentLoom 并安全重启",
        description: "agentloom update · 整体更新 TUI、OpenCode 与 Python Runtime",
        action: "update",
      }]
    : [])
  const paletteItems = createMemo(() => paletteScope() === "models"
    ? modelItems()
    : [...updateItems(), ...buildPaletteItems(snapshot()), ...modelItems()])
  const filteredPaletteItems = createMemo(() => filterPaletteItems(paletteItems(), paletteQuery()))
  const studioModelLabel = createMemo(() => {
    const model = state().studioModel
    return model ? `${model.providerName} · ${model.name}` : null
  })
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
      && (!state().assistantBusy || Boolean(state().questionRequest))
    ) {
      input.focus()
      return
    }
    input.blur()
  })

  useKeyboard((event) => {
    // Match OpenCode's selection-first copy behavior. Ctrl+Y reaches the TUI
    // consistently even in terminals that reserve Ctrl+C for interrupts.
    if (event.ctrl && event.name === "y") {
      if (copySelectedText(renderer)) event.preventDefault()
      return
    }
    if (event.ctrl && event.name === "c") {
      event.preventDefault()
      props.onExit()
      return
    }
    if (event.ctrl && event.name === "x") {
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
    if (state().questionRequest && event.name === "escape") {
      event.preventDefault()
      void props.session.rejectQuestion()
      return
    }
    const permission = state().permissionRequest
    if (permission) {
      const reply = event.name === "1"
        ? "once"
        : event.name === "2"
          ? "always"
          : event.name === "3" || event.name === "escape"
            ? "reject"
            : null
      if (reply) {
        event.preventDefault()
        void props.session.respondPermission(reply)
      }
      return
    }
    if (event.name === "escape" && state().assistantBusy && state().studioSessionID) {
      event.preventDefault()
      void props.session.interruptStudio()
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

    if (["pageup", "pagedown"].includes(event.name)) {
      if (focus() === "context" && layout().visible) navigateContext(event)
      else navigateMessages(event)
      return
    }

    const contextNavigation = focus() === "context" && ["up", "down", "home", "end"].includes(event.name)
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
    setTimeout(() => {
      if (!paletteInput || paletteInput.isDestroyed) return
      paletteInput.focus()
    }, 1)
  }

  function closePalette(restoreBuilderFocus = true) {
    setPaletteOpen(false)
    paletteInput?.blur()
    if (restoreBuilderFocus) {
      setFocus("builder")
      setTimeout(() => {
        if (!input || input.isDestroyed) return
        input.focus()
      }, 1)
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

  function navigateMessages(event: Parameters<ScrollBoxRenderable["handleKeyPress"]>[0]) {
    if (!chatScrollbox || chatScrollbox.isDestroyed) return
    event.preventDefault()
    // OpenCode v1.18.3 maps PageUp/PageDown to a half-viewport scroll.
    chatScrollbox.scrollBy(event.name === "pageup" ? -chatScrollbox.height / 2 : chatScrollbox.height / 2)
  }

  function scrollMessagesToBottom() {
    // Match OpenCode's prompt submission behavior. stickyScroll deliberately
    // stops following while the user reads older history, so a new prompt must
    // explicitly resume from the newest turn.
    setTimeout(() => {
      if (!chatScrollbox || chatScrollbox.isDestroyed) return
      chatScrollbox.scrollTo(chatScrollbox.scrollHeight)
    }, 50)
  }

  function activatePaletteItem(index = paletteSelected()) {
    const item = filteredPaletteItems()[index]
    if (!item) return
    if ("modelType" in item) {
      closePalette()
      if (state().studioEnabled) void props.session.setStudioModel(item.modelType)
      else void props.session.submit(`/model ${item.modelType}`)
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
    if (item.action === "new-application") void props.session.beginApplicationCreation()
    if (item.action === "chat") props.session.goBuilder()
    if (item.action === "refresh") void props.session.refresh()
    if (item.action === "permission-toggle") void props.session.togglePermissionMode()
    if (item.action === "update") {
      void props.session.installUpdate().then((installed) => {
        if (installed) props.onRestart?.()
      })
    }
    if (item.action === "apply") void props.session.submit("/apply")
    if (item.action === "schedules") void props.session.submit("/schedule")
  }

  function submitBuilder(value: string) {
    if (value.trim() === "/models") {
      openPalette("models")
      return
    }
    void props.session.submit(value)
    scrollMessagesToBottom()
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
          studioEnabled={state().studioEnabled}
          studioModel={studioModelLabel()}
          workspacePhase={state().workspacePhase}
          assistantBusy={state().assistantBusy}
          loopState={state().loopState}
          updatePhase={state().updatePhase}
          spinner={spinner()}
        />
        <box flexGrow={1} minHeight={0} paddingLeft={2} paddingRight={2} paddingBottom={1}>
          <BuilderView
            state={state()}
            theme={theme()}
            focus={focus() === "builder" && !paletteOpen()}
            bindInput={(value) => (input = value)}
            submit={submitBuilder}
            onFocus={() => setFocus("builder")}
            bindScrollbox={(value) => (chatScrollbox = value)}
            navigateMessages={navigateMessages}
            respondPermission={(reply) => void props.session.respondPermission(reply)}
            answerQuestion={(answer) => void props.session.respondQuestion(answer)}
            answerQuestionOption={(answer) => void props.session.respondQuestionOption(answer)}
            rejectQuestion={() => void props.session.rejectQuestion()}
            interrupt={() => void props.session.interruptStudio()}
            spinner={spinner()}
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
              spinner={spinner()}
            bindScrollbox={(value) => (contextScrollbox = value)}
            onOpenEntry={openContextEntry}
            onCreateApplication={() => {
              setFocus("builder")
              props.session.beginApplicationCreation()
              setTimeout(() => input?.focus(), 1)
            }}
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
                spinner={spinner()}
                bindScrollbox={(value) => (contextScrollbox = value)}
                onOpenEntry={openContextEntry}
                onCreateApplication={() => {
                  setFocus("builder")
                  props.session.beginApplicationCreation()
                  setTimeout(() => input?.focus(), 1)
                }}
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
  studioEnabled: boolean
  studioModel: string | null
  workspacePhase: AgentLoomSessionState["workspacePhase"]
  assistantBusy: boolean
  loopState: AgentLoomSessionState["loopState"]
  updatePhase: AgentLoomSessionState["updatePhase"]
  spinner: string
}) {
  const activity = () => {
    if (props.workspacePhase === "loading") return `${props.spinner} 正在索引项目…`
    if (props.workspacePhase === "error") return "索引失败 · /refresh 重试"
    if (props.updatePhase === "installing") return `${props.spinner} 正在整体更新…`
    if (props.loopState !== "idle") return loopStateLabel(props.loopState, props.spinner)
    if (props.assistantBusy) return `${props.spinner} AgentLoom 正在思考…`
    if (props.updatePhase === "available") return "发现可用更新 · Ctrl+X"
    return props.studioEnabled
      ? `Studio: ${props.studioModel ?? "config/llm.yaml default"}`
      : `model: ${props.model ?? "未配置"}`
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
  bindScrollbox: (scrollbox: ScrollBoxRenderable) => void
  submit: (value: string) => void
  onFocus: () => void
  navigateMessages: (event: Parameters<ScrollBoxRenderable["handleKeyPress"]>[0]) => void
  respondPermission: (reply: "once" | "always" | "reject") => void
  answerQuestion: (answer: string) => void
  answerQuestionOption: (answer: string) => void
  rejectQuestion: () => void
  interrupt: () => void
  spinner: string
}) {
  let textarea: TextareaRenderable | undefined
  const hasStudioArtifacts = () => props.state.studioTools.length > 0 || props.state.studioDiffs.length > 0
  const finalAssistantMessage = () => {
    if (!hasStudioArtifacts()) return null
    const message = props.state.messages.at(-1)
    return message?.role === "assistant" ? message : null
  }
  const messagesBeforeStudioArtifacts = () => (
    finalAssistantMessage() ? props.state.messages.slice(0, -1) : props.state.messages
  )
  const modelSummary = () => props.state.studioEnabled
    ? props.state.studioModel
      ? `${props.state.studioModel.providerName} · ${props.state.studioModel.name}`
      : "config/llm.yaml default"
    : props.state.snapshot.models.items
      .filter((item) => item.configured)
      .map((item) => `${item.type}${item.default ? "*" : ""}`)
      .join(" · ") || "未配置"

  function submit() {
    const value = textarea?.plainText.trim() ?? ""
    if (!value || (props.state.assistantBusy && !props.state.questionRequest)) return
    textarea?.clear()
    if (props.state.questionRequest) props.answerQuestion(value)
    else props.submit(value)
  }

  function requestSubmit() {
    // Match OpenCode's IME handling: Return can arrive before the final CJK
    // composition is flushed into plainText, so read the buffer two tasks later.
    setTimeout(() => setTimeout(submit, 0), 0)
  }

  return (
    <box flexGrow={1} minHeight={0}>
      <box flexDirection="row" justifyContent="space-between" flexShrink={0} marginBottom={1}>
        <text fg={props.theme.text} attributes={TextAttributes.BOLD}>AgentLoom Application Studio</text>
        <text fg={props.theme.muted}>{studioContextLabel(props.state)}</text>
      </box>
      <text flexShrink={0} fg={props.theme.muted} marginBottom={1}>
        {`创建 · 修改 · 验证 · 运行  ·  ${props.state.studioEnabled ? "Studio" : "Models"}: ${modelSummary()}`}
      </text>
      <scrollbox
        id="agentloom-chat-scrollbox"
        ref={(value: ScrollBoxRenderable) => props.bindScrollbox(value)}
        flexGrow={1}
        minHeight={0}
        stickyScroll
        stickyStart="bottom"
        scrollAcceleration={createDefaultScrollAcceleration()}
        verticalScrollbarOptions={{
          trackOptions: {
            backgroundColor: props.theme.background,
            foregroundColor: props.theme.borderActive,
          },
        }}
      >
        <box gap={1} paddingRight={1}>
          <For each={messagesBeforeStudioArtifacts()}>
            {(message) => <ChatMessageView message={message} theme={props.theme} />}
          </For>
          <For each={props.state.activities}>
            {(activity) => (
              <text fg={activity.state === "completed" ? props.theme.success : props.theme.warning}>
                {activity.state === "completed" ? "✓" : props.spinner} {activity.name}
              </text>
            )}
          </For>
          <For each={props.state.studioTools}>
            {(tool) => {
              const summary = () => studioToolOutput(tool)
              return (
                <box flexShrink={0} border={["left"]} borderColor={tool.status === "error" ? props.theme.error : props.theme.borderActive} paddingLeft={1}>
                  <text fg={tool.status === "completed" ? props.theme.success : tool.status === "error" ? props.theme.error : props.theme.warning} attributes={TextAttributes.BOLD}>
                    {tool.status === "completed" ? "✓" : tool.status === "error" ? "×" : props.spinner} {tool.source ? "子 Agent · " : ""}{tool.title ?? tool.name}
                  </text>
                  <Show when={summary()}>
                    {(output) => <text fg={props.theme.muted} wrapMode="word">{output()}</text>}
                  </Show>
                </box>
              )
            }}
          </For>
          <Show when={props.state.studioDiffs.length > 0}>
            <box flexShrink={0} border={["left"]} borderColor={props.theme.secondary} paddingLeft={1} gap={1}>
              <text fg={props.theme.secondary} attributes={TextAttributes.BOLD}>Changes</text>
              <For each={props.state.studioDiffs}>
                {(file) => (
                  <text fg={props.theme.text} wrapMode="word">
                    {file.status ?? "modified"} {file.file ?? "unknown"} +{file.additions} -{file.deletions}
                  </text>
                )}
              </For>
            </box>
          </Show>
          <Show when={finalAssistantMessage()}>
            {(message) => <ChatMessageView message={message()} theme={props.theme} />}
          </Show>
          <Show when={props.state.streamingText}>
            <box flexShrink={0} border={["left"]} borderColor={props.theme.secondary} paddingLeft={1} paddingTop={1} paddingBottom={1}>
              <text fg={props.theme.secondary} attributes={TextAttributes.BOLD}>
                {props.state.loopState === "idle" ? "子 Agent · execution log" : "AgentLoom · streaming"}
              </text>
              <text fg={props.theme.text} wrapMode="word">{props.state.streamingText}</text>
            </box>
          </Show>
          <Show when={props.state.permissionRequest}>
            {(request) => (
              <box flexShrink={0} border borderColor={props.theme.warning} paddingLeft={1} paddingRight={1} paddingTop={1} paddingBottom={1} gap={1}>
                <text fg={props.theme.warning} attributes={TextAttributes.BOLD}>{request().source ? "子 Agent 需要授权" : "需要授权"} · {request().permission}</text>
                <For each={request().patterns.slice(0, 4)}>
                  {(pattern) => <text fg={props.theme.text} wrapMode="word">{pattern}</text>}
                </For>
                <box flexDirection="row" gap={2}>
                  <text fg={props.theme.primary} attributes={TextAttributes.BOLD} onMouseDown={() => props.respondPermission("once")}>[ 1 仅本次 ]</text>
                  <text fg={props.theme.primary} attributes={TextAttributes.BOLD} onMouseDown={() => props.respondPermission("always")}>[ 2 本次会话 ]</text>
                  <text fg={props.theme.error} attributes={TextAttributes.BOLD} onMouseDown={() => props.respondPermission("reject")}>[ 3 拒绝 ]</text>
                </box>
              </box>
            )}
          </Show>
          <Show when={props.state.questionRequest}>
            {(request) => (
              <box flexShrink={0} border borderColor={props.theme.warning} paddingLeft={1} paddingRight={1} paddingTop={1} paddingBottom={1} gap={1}>
                <For each={request().questions}>
                  {(question, questionIndex) => (
                    <box flexShrink={0} gap={1}>
                      <text fg={props.theme.warning} attributes={TextAttributes.BOLD}>
                        需要你的决定 · {question.header}
                      </text>
                      <text fg={props.theme.text} wrapMode="word">{question.question}</text>
                      <For each={question.options}>
                        {(option, optionIndex) => (
                          <box
                            id={questionIndex() === 0 ? `studio-question-option-${optionIndex()}` : undefined}
                            flexShrink={0}
                            onMouseDown={() => {
                              if (request().questions.length === 1 && !question.multiple) {
                                props.answerQuestionOption(option.label)
                              }
                            }}
                          >
                            <text fg={props.theme.primary} attributes={TextAttributes.BOLD}>[ {option.label} ]</text>
                            <Show when={option.description}>
                              <text fg={props.theme.muted} wrapMode="word">  {option.description}</text>
                            </Show>
                          </box>
                        )}
                      </For>
                    </box>
                  )}
                </For>
                <text fg={props.theme.muted}>
                  {request().questions.length === 1
                    ? "在下方输入自定义答案并回车 · Esc 拒绝"
                    : `在下方输入 ${request().questions.length} 个答案，用 | 分隔 · Esc 拒绝`}
                </text>
                <text fg={props.theme.error} onMouseDown={props.rejectQuestion}>[ 拒绝回答 ]</text>
              </box>
            )}
          </Show>
          <Show when={props.state.assistantBusy && !props.state.questionRequest && !props.state.permissionRequest}>
            <box flexShrink={0} border={["left"]} borderColor={props.theme.warning} paddingLeft={1} paddingTop={1} paddingBottom={1}>
              <text fg={props.theme.warning}>{props.spinner} AgentLoom 正在思考并调用所需能力…</text>
              <text fg={props.theme.muted} onMouseDown={props.interrupt}>Esc 中止</text>
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
          focused={props.focus && (!props.state.assistantBusy || Boolean(props.state.questionRequest))}
          minHeight={1}
          maxHeight={5}
          placeholder={props.state.questionRequest
            ? props.state.questionRequest.questions.length === 1
              ? "输入你的答案"
              : `输入 ${props.state.questionRequest.questions.length} 个答案，用 | 分隔`
            : "描述你要创建或修改的 Application；输入 /help 查看命令"}
          placeholderColor={props.theme.muted}
          textColor={props.theme.text}
          focusedTextColor={props.theme.text}
          backgroundColor={props.theme.element}
          focusedBackgroundColor={props.theme.element}
          cursorColor={props.state.assistantBusy && !props.state.questionRequest ? props.theme.element : props.theme.primary}
          keyBindings={[
            { name: "return", action: "submit" },
            { name: "kpenter", action: "submit" },
            { name: "linefeed", action: "submit" },
            { name: "return", shift: true, action: "newline" },
            { name: "kpenter", shift: true, action: "newline" },
          ]}
          onKeyDown={(event) => {
            if (["pageup", "pagedown"].includes(event.name)) {
              props.navigateMessages(event)
              return
            }
            if (props.state.assistantBusy && !props.state.questionRequest) event.preventDefault()
          }}
          onSubmit={requestSubmit}
          onMouseDown={() => {
            props.onFocus()
            textarea?.focus()
          }}
        />
      </box>
      <box flexShrink={0} flexDirection="row" gap={2} paddingTop={1}>
        <text
          id="agentloom-send-control"
          fg={props.state.assistantBusy && !props.state.questionRequest ? props.theme.muted : props.theme.primary}
          attributes={props.state.assistantBusy && !props.state.questionRequest ? undefined : TextAttributes.BOLD}
          onMouseDown={requestSubmit}
        >
          {props.state.questionRequest ? "[ Enter 回复问题 ]" : "[ Enter 发送 ]"}
        </text>
        <text fg={props.theme.muted}>Ctrl+X Commands</text>
        <text fg={props.theme.muted}>Ctrl+Y 复制选中</text>
        <text fg={props.theme.muted}>/help 帮助</text>
      </box>
    </box>
  )
}

function loopStateLabel(state: AgentLoomSessionState["loopState"], spinner: string): string {
  if (state === "thinking") return `${spinner} 正在思考…`
  if (state === "tool") return `${spinner} 正在调用工具…`
  if (state === "validating") return `${spinner} 正在校验 Application…`
  if (state === "running") return `${spinner} 正在运行 Application…`
  if (state === "waiting_permission") return "等待权限选择"
  if (state === "failed") return "Agent Loop 失败"
  return "空闲"
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
  onCreateApplication: () => void
  onAnalyzeRun: () => void
  onClose: () => void
  onFocus: () => void
}) {
  const catalogDetail = createMemo(() => workspaceEntityDetail(props.state.snapshot, props.state.route))
  const agentDetail = createMemo(() => {
    if (props.state.route.type !== "agent" || !props.state.applicationDetail) return null
    const effective = effectiveAgentDetail(props.state.applicationDetail, props.state.route.agentID)
    if (!effective) return null
    const runtime = catalogDetail()?.sections.find((section) => section.title === "运行")
    return {
      ...effective,
      sections: runtime
        ? [effective.sections[0]!, runtime, ...effective.sections.slice(1)]
        : effective.sections,
    }
  })
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
        <Match when={props.state.route.type === "application"}>
          <DetailView
            title="Application"
            subtitle={props.state.applicationDetail?.application.name ?? "正在读取"}
            sections={props.state.applicationDetail ? applicationDetailSections(props.state.applicationDetail) : []}
            busy={props.state.detailBusy}
            spinner={props.spinner}
            theme={props.theme}
            bindScrollbox={props.bindScrollbox}
          />
        </Match>
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
        <Match when={props.state.route.type === "agent" && agentDetail()}>
          <DetailView
            title={agentDetail()?.title ?? "Agent"}
            subtitle={agentDetail()?.subtitle ?? "正在读取"}
            sections={agentDetail()?.sections ?? []}
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
        <Match when={props.state.route.type !== "application" && catalogDetail()}>
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
            onCreateApplication={props.onCreateApplication}
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
        <text fg={props.theme.muted}>Ctrl+X Commands · Esc 返回</text>
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
  onCreateApplication: () => void
}) {
  const active = () => props.groups.runs.filter((entry) => entry.status === "running")
  const count = (status: SidebarRunEntry["status"]) => (
    props.groups.runs.filter((entry) => entry.status === status).length
  )
  const recent = () => recentRunEntries(props.groups.runs)
  const globalSkills = () => props.groups.skills.filter((entry) => {
    const skill = props.state.snapshot.skills.find((candidate) => candidate.id === entry.skillID)
    return skill?.origin === "global"
  })
  const visibleSkills = () => globalSkills().slice(0, 5)
  const applicationCountLabel = () => {
    const count = props.groups.applications.length
    return `${count} ${count === 1 ? "Application" : "Applications"}`
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
          <text fg={props.theme.text} attributes={TextAttributes.BOLD}>Application Studio</text>
          <text fg={props.theme.muted}>{props.state.snapshot.project.name}</text>
        </box>
        <text fg={props.theme.primary} attributes={TextAttributes.BOLD}>Applications</text>
        <box flexShrink={0} border={["left"]} borderColor={props.theme.primary} paddingLeft={1}>
          <text fg={props.theme.text}>
            {applicationCountLabel()} · {globalSkills().length} Global Skills
          </text>
          <text
            fg={props.theme.primary}
            attributes={TextAttributes.BOLD}
            onMouseDown={props.onCreateApplication}
          >
            + New Application
          </text>
        </box>
        <For each={props.groups.applications}>
          {(entry, index) => (
            <box flexShrink={0} paddingBottom={1} onMouseDown={() => props.onOpenEntry(entry)}>
              <text
                id={`agentloom-application-entry-${index()}`}
                fg={props.theme.text}
                wrapMode="char"
              >
                {entry.title}
              </text>
              <text fg={props.theme.muted} wrapMode="word">
                {entry.subtitle} · 点击打开
              </text>
            </box>
          )}
        </For>
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
            Global Skills ({globalSkills().length})
          </text>
          <For each={visibleSkills()}>
            {(entry) => (
              <box flexShrink={0} paddingBottom={1} onMouseDown={() => props.onOpenEntry(entry)}>
                <text fg={props.theme.text} wrapMode="none" truncate>{entry.title}</text>
                <text fg={props.theme.muted} wrapMode="none" truncate>{entry.subtitle} · 点击查看</text>
              </box>
            )}
          </For>
          <Show when={globalSkills().length > visibleSkills().length}>
            <text fg={props.theme.muted}>
              还有 {globalSkills().length - visibleSkills().length} 个 · Ctrl+X 查看全部
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

function studioContextLabel(state: AgentLoomSessionState): string {
  const target = state.studioTarget?.type === "application"
    ? state.studioTarget.applicationID
    : state.studioTarget?.type === "new"
      ? "New Application"
      : "Choose an Application"
  const permission = state.permissionMode === "full_access" ? "Full Access" : "Application Only"
  return `${target} · ${permission}`
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
              ? "Studio 模型 · config/llm.yaml"
              : "Commands · Models · Applications · Main Agents · Skills · Schedules · Runs"}
          </text>
          <text fg={props.theme.muted}>esc</text>
        </box>
        <box flexShrink={0} marginTop={1} marginBottom={1} border={["bottom"]} borderColor={props.theme.border}>
          <input
            ref={(value: InputRenderable) => props.bindInput(value)}
            value={props.query}
            onInput={props.onQuery}
            placeholder={props.scope === "models"
              ? "搜索并选择 llm.yaml 模型类型"
              : "搜索 Model、Application、主 Agent、Skill、Schedule、Run 或命令"}
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
                        <text
                          fg={selected() ? props.theme.text : props.theme.muted}
                          wrapMode={item.category === "Applications" ? "char" : "none"}
                          truncate={item.category !== "Applications"}
                        >
                          {item.title}
                        </text>
                        <Show when={item.category !== "Applications"}>
                          <text fg={props.theme.primary}>
                            {item.category === "Agents" ? "Main Agent" : item.category}
                          </text>
                        </Show>
                      </box>
                      <box flexDirection="row" justifyContent="space-between">
                        <text fg={props.theme.muted} wrapMode="none" truncate>{item.description}</text>
                        <Show when={item.category === "Applications"}>
                          <text fg={props.theme.primary}>Application</text>
                        </Show>
                      </box>
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
      <text fg={props.theme.muted}>Ctrl+Y 复制选中 · Ctrl-C 退出</text>
    </box>
  )
}
