import { useEffect, useState } from "react";

export function navigate(to: string) {
  if (to === location.pathname + location.search) return;
  history.pushState({}, "", to);
  window.dispatchEvent(new Event("az:nav"));
  window.scrollTo({ top: 0 });
}

export function usePath() {
  const [p, setP] = useState(location.pathname);
  useEffect(() => {
    const on = () => setP(location.pathname);
    window.addEventListener("popstate", on);
    window.addEventListener("az:nav", on);
    return () => { window.removeEventListener("popstate", on); window.removeEventListener("az:nav", on); };
  }, []);
  return p;
}

export function linkProps(to: string) {
  return {
    href: to,
    onClick: (e: React.MouseEvent) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      e.preventDefault();
      navigate(to);
    },
  };
}
