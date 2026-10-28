/** Shapes of what the channel page reads (web/app.py, tuning.py, preview.py, compiler.annotations). */

export type Rule = { id?: string; label?: string; rule: string; phases?: string[]; clause_ids?: string[] };
export type Facet = { key: string; label: string; type: string; description: string; clause_ids?: string[] };
export type Playbook = {
  id: number; prompt_id: number | null; version: number; status: string; origin: string | null;
  created_at: string; activated_at: string | null; rules: Rule[]; facets: Facet[]; audit: any; article_style?: string | null;
};
export type Prompt = { id: number; version: number; note: string | null; created_at: string; text: string; origin?: string | null };
export type Scan = { at: string; found: number; queued: number; registered: number; have: number; short: number; error: string | null };
export type Ingest = {
  enabled?: boolean; min_minutes?: number; since?: string; last_poll?: string; every_minutes?: number; scans?: Scan[]; scanning?: boolean;
  next_at?: string | null;  // the worker's next round over the channels
};
/** what a channel has produced (web/app.py _channel_stats) */
export type ChannelStats = {
  issues: number; month: number; last_month: number; units: number; clips: number; seconds: number; notes: number; tokens: number;
  delivered: number; registered: number; failed: number; waiting: number; running: number; queued: number;
  weeks: number[]; week_start: string;
  last: { id: string; title: string | null; headline: string | null; published_at: string | null; delivery: string; finished_at: string | null } | null;
};
export type Channel = {
  id: string; name: string; stage: "TUNING" | "LOCKED"; mode: "TRACKED" | "DISCOVERY"; youtube_channel_id: string | null; why: string | null;
  playbooks: Playbook[]; prompts: Prompt[]; sources: number; last_source: string | null; ingest: Ingest;
  notion: { configured: boolean; database_title?: string | null; template?: string | null; test_copy?: boolean };
  samples: number; samples_busy: number; compile: { state: "done" | "running" | "failed"; error?: string; since?: string } | null;
  pending_playbook: number | null; blocked: number; held: number; stats: ChannelStats;
};
export type Board = {
  stats: ChannelStats; issues: any[]; ingest: Ingest;
  backfill: {
    pending: number; seconds: number; running: { id: string; production: string; wait_reason: string | null; title: string | null } | null;
    held: "weekly" | "5h" | null;  // the quota window that holds the waiting 补录 back
  };
  pace: number; runway: { per_issue: number; issues: number; remaining: number; reset_at: string | null } | null;
};
export type Candidate = {
  id: string; title: string | null; published: string; duration?: number | null; approx?: boolean;
  sample: boolean; produced: boolean; here: boolean; library: any; queued: boolean; short: boolean;
};
export type ChannelsResp = { channels: Channel[]; universal: Playbook | null; decisions: any[] };

export type Run = {
  id: string; production: string; stage: string | null; wait_reason: string | null; error_class: string | null;
  created_at: string; finished_at: string | null; playbook_id: number | null; version: number | null; playbook_status: string | null;
  progress: [number, number]; units: number | null; must: number; clips: number; notes: number; chars: number;
  track: [number, number, number][]; flag_at: number[]; eta_seconds: number | null; error_line: string | null;
};
export type Sample = {
  id: string; title: string; url: string | null; duration: number | null; published_at: string | null; added_at: string;
  current_run_id: string | null; adopt: { run_id: string | null; pinned: boolean }; delivery: string; notion_url: string | null;
  runs: Run[]; stale: boolean; busy: boolean;
};
export type WbPlaybook = {
  id: number; version: number; status: string; origin: string | null; created_at: string; activated_at: string | null;
  prompt_version: number | null; note: string | null; adopt_count: number; missing: number; est_minutes: number;
};
export type Workbench = {
  channel: { id: string; name: string; stage: string; mode: string; youtube_channel_id: string | null };
  playbooks: WbPlaybook[]; draft_id: number | null; active_id: number | null; samples: Sample[];
  queue: { id: number; source_id: string; playbook_id: number; version: number; title: string | null }[];
};

export type Seg = { t: string; b?: boolean; i?: boolean; c?: boolean; href?: string };
export type Block = {
  key: string; type: string; text?: Seg[]; children?: Block[]; rows?: Seg[][][]; header?: boolean; url?: string; emoji?: string;
  checked?: boolean; caption?: string; asset_id?: number | null; origin: string; unit_ids: string[]; tpl: boolean;
};
export type Part = { text: string; var?: string };
export type Mapping = { name: string; type: string; source: string; value?: any };
export type PropRow = {
  id: string; name: string; type: string; writable: boolean; mapping: Mapping | null; problem: string | null;
  colors: Record<string, string>; value?: any; deleted?: boolean;
};
export type Preview = {
  configured: boolean; empty?: boolean; database: { id: string; url: string; title: string };
  source: { id: string; title: string; url: string }; run_id: string; title: Part[]; heading: Part[];
  icon: { emoji?: string; url?: string } | null; properties: PropRow[]; problems: Record<string, string>; warnings: Record<string, string>;
  blocks: Block[]; body_error: string | null; context: Record<string, string>;
};
export type NotionRow = PropRow & { options?: string[]; relation_db?: string; sources: string[]; sample: any };
export type NotionView = {
  configured: boolean; database: { id: string; url: string; title: string }; config: { template: string; heading: string; icon: string };
  rows: NotionRow[]; problems: number; warnings: Record<string, string>; page_titles: Record<string, string | null>;
  templates: Record<string, string>; source_labels: Record<string, string>; template_vars: Record<string, string>; access_labels: Record<string, string>;
  repick?: string[];
};
export type Draft = { properties: Record<string, Mapping>; template: string; heading: string; icon: string };

export type Unit = { id: string; title: string; start: number; end: number; learning_priority?: number; tickers?: string[] };
export type SourceDetail = {
  source: { id: string; title: string | null; url: string | null; duration: number | null; published_at: string | null; channel_title: string | null; info: any; notion_url?: string | null };
  run: { id: string } | null; units: Unit[]; assets: { id: number; unit_id: string | null; kind: string; status: string; start: number | null }[];
  sections: { id: string; kind: string; unit_ids: string[] }[]; flags: any[];
  playback?: boolean;  // a 360p copy kept for tuning (playback.py)
};
export type Compare = {
  a_units: (Unit & { change: string })[]; b_units: (Unit & { change: string })[];
  counts: { new: number; split: number; merge: number; gone: number };
  rules_added: string[]; rules_removed: string[]; rules_changed: { from: string; to: string; label: string }[];
};
export type Clause = { id: string; text: string; section: string; start: number | null; end: number | null; disposition: string | null; rule_id: string | null; reason: string | null };
export type Annotations = {
  playbook: { id: number; version: number; status: string; origin: string | null };
  prompt: { id: number; version: number; note: string | null; text: string } | null; has_provenance: boolean; has_labels: boolean;
  clauses: Clause[]; rules: (Rule & { label: string })[]; facets: Facet[]; article_style: string | null;
  conflicts: { clause_id: string; rule_id?: string; issue: string }[];
  coverage: { clauses: number; used: number; system: number; redundant: number; ignored: number };
  vs_previous: { version: number; added: { id?: string; label: string; rule: string }[]; removed: { id?: string; label: string; rule: string }[];
    changed: { from: { id?: string; label: string; rule: string }; to: { id?: string; label: string; rule: string } }[] } | null;
};
export type LockRow = {
  source_id: string; title: string; delivery: string; adopted: { run_id: string; version: number; pinned: boolean } | null;
  running: { run_id: string; version: number; state: string } | null; queued: number[];
  library: { url: string; title: string; owned: boolean } | null; can_replace: boolean;
};
export type LockPreview = {
  channel: { id: string; name: string; stage: string }; playbook: { id: number; version: number; status: string }; tested: boolean;
  candidates: { id: number; version: number; status: string; tested: boolean }[]; conflicts: { clause_id: string; rule_id?: string; issue: string }[];
  samples: LockRow[]; target: { configured: boolean; title: string; test_copy: boolean };
  ingest: { available: boolean; enabled: boolean; min_minutes: number }; library_checked: boolean; library_error?: string;
};
export type Issue = {
  id: string; title: string; published_at: string | null; duration: number | null; delivery: string; delivery_reason: string | null;
  notion_url: string | null; current_run_id: string; units: number | null; notes: number;
};
