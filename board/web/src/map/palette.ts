// Scene colours for the day and dusk variants (prefers-color-scheme). The agent colours are the board's
// --codex and --claude hues (styles.css), lighter at dusk like the board's dark theme.

export interface Palette {
  sky: string;
  water: string;
  shallow: string;
  sand: string;
  grass: string;
  yard: string;
  path: string;
  wood: string;
  woodDark: string;
  metal: string;
  metalDark: string;
  canvas: string;
  canvasAlt: string;
  rock: string;
  leaf: string;
  leafDark: string;
  trunk: string;
  paper: string;
  crate: string;
  ready: string;
  alarmGround: string;
  screen: string;
  screenOff: string;
  lamp: string;
  lampOff: string;
  beacon: string;
  standLight: string;
  gateLight: string;
  select: string;
  codex: string;
  codexSoft: string;
  codexDark: string;
  claude: string;
  claudeSoft: string;
  claudeDark: string;
  shadow: string;
  /** Lights. */
  hemiSky: string;
  hemiGround: string;
  hemi: number;
  sun: string;
  sunIntensity: number;
  shadowOpacity: number;
}

export type Tone = { [K in keyof Palette]: Palette[K] extends string ? K : never }[keyof Palette];

export const DAY: Palette = {
  sky: "#cfe1ea",
  water: "#79afc8",
  shallow: "#9fcfd6",
  sand: "#e6d6ac",
  grass: "#a3c47f",
  yard: "#d8c7a0",
  path: "#e2d3ad",
  wood: "#a67c55",
  woodDark: "#7a5a3d",
  metal: "#9aa4ae",
  metalDark: "#646e79",
  canvas: "#d9cfb6",
  canvasAlt: "#b9c9b0",
  rock: "#a7a59d",
  leaf: "#6f9e57",
  leafDark: "#557f45",
  trunk: "#8a6446",
  paper: "#f4f1e8",
  crate: "#e0bd82",
  ready: "#2f9a5c",
  alarmGround: "#eaa79c",
  screen: "#45d39a",
  screenOff: "#38424c",
  lamp: "#ffd25e",
  lampOff: "#9b9789",
  beacon: "#e5352b",
  standLight: "#f2b632",
  gateLight: "#f2b632",
  select: "#2563d0",
  codex: "#2459b8",
  codexSoft: "#d5e2f8",
  codexDark: "#1b3f80",
  claude: "#a84f2c",
  claudeSoft: "#f5ddcf",
  claudeDark: "#7c3a20",
  shadow: "#1c2a33",
  hemiSky: "#ffffff",
  hemiGround: "#8f8a73",
  hemi: 1.9,
  sun: "#fff4e0",
  sunIntensity: 1.7,
  shadowOpacity: 0.2,
};

export const DUSK: Palette = {
  sky: "#262d40",
  water: "#2c4560",
  shallow: "#3a5a70",
  sand: "#8f8169",
  grass: "#55704c",
  yard: "#7e7058",
  path: "#887a61",
  wood: "#7e5f45",
  woodDark: "#5c4433",
  metal: "#77808c",
  metalDark: "#4f5763",
  canvas: "#a59c88",
  canvasAlt: "#8b9a88",
  rock: "#76756f",
  leaf: "#46663d",
  leafDark: "#37522f",
  trunk: "#5f4635",
  paper: "#d7d3c8",
  crate: "#b8975f",
  ready: "#4fbf80",
  alarmGround: "#8a3c35",
  screen: "#5cf0b2",
  screenOff: "#262d35",
  lamp: "#ffe08a",
  lampOff: "#4c4b47",
  beacon: "#ff4b3e",
  standLight: "#ffc84a",
  gateLight: "#ffc84a",
  select: "#6b9bff",
  codex: "#7ea6f5",
  codexSoft: "#dde7fb",
  codexDark: "#4d6fb4",
  claude: "#e59472",
  claudeSoft: "#f6dccf",
  claudeDark: "#b06446",
  shadow: "#05080c",
  hemiSky: "#a9b6e0",
  hemiGround: "#3a3530",
  hemi: 1.5,
  sun: "#ffb98a",
  sunIntensity: 1.25,
  shadowOpacity: 0.3,
};
