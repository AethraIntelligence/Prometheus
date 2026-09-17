/**
 * What the runtime says about providers, models and where work goes.
 *
 * `has_key` and never the key. The core does not return a credential from any
 * endpoint, so there is nothing here to accidentally render: a window that can
 * display a key is a window that puts one in a screenshot.
 *
 * `used_for` and `usable` arrive already decided, like `requires_approval` on
 * an integration. Working out here which model a kind of work would go to would
 * be a second router written in TypeScript, disagreeing quietly with the real one.
 */

export interface ProviderKind {
  name: string;
  label: string;
  needs_credential: boolean;
  default_base_url: string;
}

export interface Connection {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  description: string;
  needs_credential: boolean;
  /** Whether a credential is stored. Never the credential. */
  has_key: boolean;
  /** Decided by the core: whether a client could be built from this at all. */
  usable: boolean;
}

export interface ModelEntry {
  name: string;
  provider: string;
  model: string;
  connection: string;
  capabilities: string[];
  context_tokens: number;
  input_cost_per_1k_usd: number;
  output_cost_per_1k_usd: number;
  quality: number;
  dimensions: number;
  /** Decided by the core: whether it turns text into vectors. */
  embeds: boolean;
  /** Decided by the core: whether it can be given text to write. */
  generates_text: boolean;
  /** Kinds of work currently routed here, decided by the core. */
  used_for: string[];
  /** The model's contract, as the core states it: quality band, whether its
   * prompts leave this machine, and typical latency (0 is unknown). */
  tier?: string;
  privacy?: string;
  latency_ms?: number;
}

/**
 * What a connection's runner has, as the core found it.
 *
 * `from_disk` arrives decided: a runner that is not up but whose models are on
 * this disk is the case the form has to explain, and working it out here from
 * the other fields would be the window deciding what the runtime meant.
 */
export interface InstalledModels {
  models: string[];
  /** Whether this kind of provider can be asked at all. False for a hosted one. */
  supported: boolean;
  /** Whether the runner answered just now. */
  reachable: boolean;
  from_disk: boolean;
  /** Which runner answered or whose models these are, for a person to read. */
  runner: string;
  address: string;
}

/**
 * What the runtime recommends connecting, and how far this machine has got.
 *
 * Every name in it - providers, models, links - arrives from the runtime; this
 * window names none (`architecture.test.ts`). `connection` and `applied` are
 * decided by the core per setup.
 */
export interface SetupStep {
  text: string;
  /** "link" opens `url`, "connect" adds a connection, "apply" adds the models. */
  action: "" | "link" | "connect" | "apply";
  url: string;
  command: string;
}

export interface RecommendedModel {
  name: string;
  role: string;
  model: string;
  why: string;
  capabilities: string[];
  context_tokens: number;
  free: boolean;
  input_cost_per_1m_usd: number;
  output_cost_per_1m_usd: number;
  /** Kinds of work this model is given when the setup is applied. */
  route: string[];
}

export interface Setup {
  id: string;
  title: string;
  badge: string;
  summary: string;
  kind: string;
  connection_name: string;
  good_for: string;
  /** The connection of this kind already on the machine, or "". */
  connection: string;
  /** Whether every model of the setup is already in the catalog. */
  applied: boolean;
  steps: SetupStep[];
  models: RecommendedModel[];
  cautions: string[];
}

export interface ProviderAdvice {
  kind: string;
  label: string;
  verdict: "RECOMMENDED" | "SUPPORTED" | "NOT_YET";
  text: string;
}

export interface ProviderGuide {
  intro: string;
  checked: string;
  setups: Setup[];
  providers: ProviderAdvice[];
  requirements: { title: string; text: string }[];
}

export interface ProviderSettings {
  kinds: ProviderKind[];
  connections: Connection[];
  models: ModelEntry[];
  /** Kind of work -> catalog entry name. */
  defaults: Record<string, string>;
  guide: ProviderGuide | null;
}
