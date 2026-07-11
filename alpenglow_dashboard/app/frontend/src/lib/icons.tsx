/* Phosphor icon resolver.
 *
 * The backend returns Phosphor glyph *names* (e.g. "ph-images", "images") for
 * service/category icons; the design handoff references icons by the same
 * `ph-*` names the prototype used. This module maps those strings to the
 * `@phosphor-icons/react` components so views (C2–C4) can render
 * `<Icon name={service.icon} />` without each maintaining its own switch.
 *
 * Add new glyphs here as needed — the handoff's icon list is the vocabulary.
 */

import type { Icon as PhosphorIcon } from "@phosphor-icons/react";
import {
  AddressBookIcon,
  ArchiveIcon,
  ArrowClockwiseIcon,
  ArrowFatUpIcon,
  ArrowLeftIcon,
  ArrowRightIcon,
  ArrowSquareOutIcon,
  ArrowsClockwiseIcon,
  BookmarkSimpleIcon,
  BrainIcon,
  BriefcaseIcon,
  BroadcastIcon,
  ChartLineIcon,
  CheckCircleIcon,
  CircleIcon,
  ClockIcon,
  CloudIcon,
  CookingPotIcon,
  CopyIcon,
  CubeIcon,
  CubeTransparentIcon,
  DatabaseIcon,
  FileTextIcon,
  FilmSlateIcon,
  FolderOpenIcon,
  FunnelIcon,
  GaugeIcon,
  GitBranchIcon,
  GlobeHemisphereWestIcon,
  GlobeIcon,
  HardDrivesIcon,
  HeartbeatIcon,
  HouseIcon,
  HouseLineIcon,
  ImagesIcon,
  InfoIcon,
  KeyIcon,
  LightningIcon,
  LinkIcon,
  MagnifyingGlassIcon,
  MemoryIcon,
  MonitorIcon,
  MountainsIcon,
  MusicNotesIcon,
  NoteIcon,
  PlayCircleIcon,
  PlayIcon,
  PlugsIcon,
  PlusIcon,
  PulseIcon,
  RssIcon,
  ShieldCheckIcon,
  ShieldIcon,
  ShieldStarIcon,
  SpinnerIcon,
  SquaresFourIcon,
  StackIcon,
  StopIcon,
  TagIcon,
  TerminalWindowIcon,
  UserIcon,
  VideoCameraIcon,
  WarningIcon,
  WrenchIcon,
  XIcon,
  YoutubeLogoIcon,
} from "@phosphor-icons/react";

const MAP: Record<string, PhosphorIcon> = {
  "address-book": AddressBookIcon,
  archive: ArchiveIcon,
  "arrow-clockwise": ArrowClockwiseIcon,
  "arrow-fat-up": ArrowFatUpIcon,
  "arrow-left": ArrowLeftIcon,
  "arrow-right": ArrowRightIcon,
  "arrow-square-out": ArrowSquareOutIcon,
  "arrows-clockwise": ArrowsClockwiseIcon,
  "bookmark-simple": BookmarkSimpleIcon,
  brain: BrainIcon,
  briefcase: BriefcaseIcon,
  broadcast: BroadcastIcon,
  "chart-line": ChartLineIcon,
  "check-circle": CheckCircleIcon,
  circle: CircleIcon,
  clock: ClockIcon,
  cloud: CloudIcon,
  "cooking-pot": CookingPotIcon,
  copy: CopyIcon,
  cube: CubeIcon,
  database: DatabaseIcon,
  "file-text": FileTextIcon,
  "film-slate": FilmSlateIcon,
  "folder-open": FolderOpenIcon,
  funnel: FunnelIcon,
  gauge: GaugeIcon,
  "git-branch": GitBranchIcon,
  globe: GlobeIcon,
  "globe-hemisphere-west": GlobeHemisphereWestIcon,
  "hard-drives": HardDrivesIcon,
  heartbeat: HeartbeatIcon,
  house: HouseIcon,
  "house-line": HouseLineIcon,
  images: ImagesIcon,
  info: InfoIcon,
  key: KeyIcon,
  lightning: LightningIcon,
  link: LinkIcon,
  "magnifying-glass": MagnifyingGlassIcon,
  memory: MemoryIcon,
  monitor: MonitorIcon,
  mountains: MountainsIcon,
  "music-notes": MusicNotesIcon,
  note: NoteIcon,
  play: PlayIcon,
  "play-circle": PlayCircleIcon,
  plugs: PlugsIcon,
  plus: PlusIcon,
  pulse: PulseIcon,
  rss: RssIcon,
  shield: ShieldIcon,
  "shield-check": ShieldCheckIcon,
  "shield-star": ShieldStarIcon,
  spinner: SpinnerIcon,
  "squares-four": SquaresFourIcon,
  stack: StackIcon,
  stop: StopIcon,
  tag: TagIcon,
  "terminal-window": TerminalWindowIcon,
  user: UserIcon,
  "video-camera": VideoCameraIcon,
  warning: WarningIcon,
  wrench: WrenchIcon,
  x: XIcon,
  "youtube-logo": YoutubeLogoIcon,
};

/** Fallback for unknown glyph names — a neutral placeholder, never a crash. */
const FALLBACK: PhosphorIcon = CubeTransparentIcon;

/** Normalize "ph ph-images" / "ph-images" / "images" → "images". */
function normalize(name: string | null | undefined): string {
  if (!name) return "";
  return name
    .replace(/\bph-fill\b/g, "")
    .replace(/\bph\b/g, "")
    .trim()
    .replace(/^ph-/, "");
}

/** Resolve a glyph name to a Phosphor component (never throws). */
export function iconFor(name: string | null | undefined): PhosphorIcon {
  return MAP[normalize(name)] ?? FALLBACK;
}

export type IconProps = {
  name: string | null | undefined;
  size?: number;
  weight?: "thin" | "light" | "regular" | "bold" | "fill" | "duotone";
  color?: string;
  className?: string;
  style?: React.CSSProperties;
};

/** Render a Phosphor icon by name string. */
export function Icon({ name, ...rest }: IconProps) {
  const Cmp = iconFor(name);
  return <Cmp {...rest} />;
}
