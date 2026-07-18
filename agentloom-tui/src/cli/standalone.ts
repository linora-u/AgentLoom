import "@opentui/solid/preload"

const { main } = await import("./main")

process.exitCode = await main()
