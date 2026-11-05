import { createApp } from "vue";
import App from "./App.vue";
import { createBoard } from "./store/board";
import { serverSource, type BoardSource } from "./lib/source";
import "./styles.css";

async function main() {
  // Vite replaces MODE at build time, so the fixtures never reach a production bundle.
  const source: BoardSource =
    import.meta.env.MODE === "fixture" ? (await import("./fixtures/source")).fixtureSource() : serverSource();
  createApp(App, { board: createBoard(source) }).mount("#app");
}

void main();
