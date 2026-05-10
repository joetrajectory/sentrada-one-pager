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

  // Topbar: solidifies once the user has scrolled past the very top.
  const topbar = document.querySelector(".topbar");
  if (topbar) {
    const setScrolled = () => {
      topbar.classList.toggle("scrolled", window.scrollY > 60);
    };
    setScrolled();
    window.addEventListener("scroll", setScrolled, { passive: true });
  }

  // Sticky mobile CTA: visible once the user has scrolled past the hero,
  // hidden again once the invitation's own CTA is on screen.
  const mobileCta = document.querySelector(".mobile-cta");
  const invitationCta = document.querySelector(".invitation-cta .cta");
  if (mobileCta && invitationCta) {
    let pastHero = false;
    let invitationVisible = false;

    const update = () => {
      const shouldHide = !pastHero || invitationVisible;
      mobileCta.classList.toggle("is-hidden", shouldHide);
    };

    if ("IntersectionObserver" in window) {
      const heroSentinel = document.querySelector(".hero");
      if (heroSentinel) {
        new IntersectionObserver(([entry]) => {
          pastHero = !entry.isIntersecting && entry.boundingClientRect.top < 0;
          update();
        }, { threshold: 0 }).observe(heroSentinel);
      } else {
        pastHero = true;
      }

      new IntersectionObserver(([entry]) => {
        invitationVisible = entry.isIntersecting;
        update();
      }, { threshold: 0.25 }).observe(invitationCta);
    } else {
      pastHero = true;
      update();
    }

    update();
  }
})();
