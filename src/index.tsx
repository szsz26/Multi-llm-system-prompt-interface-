#!/usr/bin/env node
import React from "react";
import { render } from "ink";
import { loadConfig } from "./config.js";
import { ConversationStore } from "./conversation.js";
import { buildAdapters, buildDemoAdapters } from "./providers/index.js";
import { App } from "./ui/App.js";

const args = process.argv.slice(2);
const demo = args.includes("--demo");
const sessionName = args.find((a) => !a.startsWith("--")) ?? (demo ? "demo" : "default");
const config = loadConfig();
const store = new ConversationStore(sessionName);
const adapters = demo ? buildDemoAdapters() : buildAdapters(config, store);

if (adapters.length === 0) {
  process.stderr.write("No providers enabled in config.json.\n");
  process.exit(1);
}

render(<App adapters={adapters} store={store} config={config} />);
