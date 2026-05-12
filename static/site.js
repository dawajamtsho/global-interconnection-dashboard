document.documentElement.classList.add("js-motion");

const revealTargets = document.querySelectorAll(
  ".hero, .top-nav, .panel, .kpi-panel, .story-card, .policy-list-item, .page-chip, .mode-pill"
);

for (const target of revealTargets) {
  target.classList.add("reveal-target");
}

function revealIfVisible(target) {
  const rect = target.getBoundingClientRect();
  const inViewport = rect.top < window.innerHeight * 0.96 && rect.bottom > 0;
  if (inViewport) {
    target.classList.add("is-visible");
    return true;
  }
  return false;
}

const observer = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      }
    }
  },
  { threshold: 0.12, rootMargin: "0px 0px -32px 0px" }
);

for (const target of revealTargets) {
  if (!revealIfVisible(target)) {
    observer.observe(target);
  }
}
