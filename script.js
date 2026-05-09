(function () {
  "use strict";

  // Reveal on scroll
  const reveals = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && reveals.length) {
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          io.unobserve(entry.target);
        }
      }
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    reveals.forEach((el) => io.observe(el));
  } else {
    reveals.forEach((el) => el.classList.add("is-visible"));
  }

  // CTA tracking. Fires a Plausible custom event when the analytics script is loaded.
  // Replace data-domain in index.html and uncomment the script tag to enable.
  document.querySelectorAll("[data-cta]").forEach((el) => {
    el.addEventListener("click", () => {
      const where = el.getAttribute("data-cta") || "unknown";
      if (window.plausible) {
        window.plausible("CTA Click", { props: { location: where } });
      }
    });
  });
})();
