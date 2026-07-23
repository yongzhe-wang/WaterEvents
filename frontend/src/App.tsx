// WaterEvents viewer — TWO pages only, both list views: Events + Media. Stripped from the old ir-data-viewer to the
// simplest possible shell (no login gate, no telemetry, no company rail). Reuses the glass sidebar + table styling
// from styles.css. {USER 2026-07-23 "clear it to only keep two pages, one is the event one is the media, both list
// view, simplest as possible ... use this [existing design] ... follow our current json and table design"}.
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import EventsView from "./EventsView";
import MediaView from "./MediaView";

// Minimal stroke icons (currentColor) matching the old glass nav.
const svg = (inner: ReactNode): ReactNode => (
  <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor"
       strokeWidth="1.35" strokeLinecap="round" strokeLinejoin="round">{inner}</svg>
);
const ICONS = {
  events: svg(<><rect x="2" y="3" width="12" height="11" rx="1.5" /><path d="M2 6.5h12M5 1.5v2.5M11 1.5v2.5" /></>),
  media: svg(<><rect x="2" y="3" width="12" height="10" rx="1.5" /><path d="M6.5 6l4 2.5-4 2.5V6z" /></>),
};

// Two real routes so each page is deep-linkable + back/forward works (a minimal history-API router, no react-router).
type View = "events" | "media";
const ROUTES: Record<View, string> = { events: "/events", media: "/media" };
function pathToView(p: string): View { return p.replace(/\/+$/, "") === "/media" ? "media" : "events"; }

function NavItem({ label, icon, active, href, onClick }:
  { label: string; icon: ReactNode; active: boolean; href: string; onClick: () => void }) {
  return (
    <a className={`nav-item${active ? " active" : ""}`} href={href}
       onClick={(e) => { if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return; e.preventDefault(); onClick(); }}>
      <span className="nav-ico">{icon}</span>
      <span className="nav-label">{label}</span>
    </a>
  );
}

export default function App() {
  const [view, setViewState] = useState<View>(() => pathToView(window.location.pathname));
  const setView = (v: View) => {
    setViewState(v);
    if (window.location.pathname !== ROUTES[v]) window.history.pushState({ view: v }, "", ROUTES[v]);
  };
  useEffect(() => {
    const onPop = () => setViewState(pathToView(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="os-chip">
          <span className="os-dot" />
          <span className="os-name">waterevents</span>
          <span className="os-ver">live</span>
        </div>
        <nav className="nav">
          <NavItem label="Events" icon={ICONS.events} active={view === "events"} href={ROUTES.events} onClick={() => setView("events")} />
          <NavItem label="Media" icon={ICONS.media} active={view === "media"} href={ROUTES.media} onClick={() => setView("media")} />
        </nav>
      </aside>
      <main className="stage">
        {view === "media" ? <MediaView /> : <EventsView />}
      </main>
    </div>
  );
}
