(function () {
  function unavailablePlaceholder() {
    var svg = [
      "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='220'>",
      "<rect width='100%' height='100%' fill='#F8FAFC'/>",
      "<rect x='8' y='8' width='304' height='204' rx='12' fill='none' stroke='#CBD5E1' stroke-width='3'/>",
      "<text x='50%' y='46%' dominant-baseline='middle' text-anchor='middle' ",
      "fill='#64748B' font-family='Arial' font-size='16' font-weight='700'>Foto tidak tersedia</text>",
      "</svg>"
    ].join("");
    return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  }

  function markImageFailed(img, failedSrc) {
    if (!img) return;
    img.onerror = null;
    img.dataset.failedSrc = failedSrc || img.getAttribute("data-src") || "";
    img.dataset.loaded = "0";
    img.classList.add("image-unavailable");
    img.title = "Foto tidak tersedia";
    img.alt = "Foto tidak tersedia";
    img.src = unavailablePlaceholder();
  }

  function loadImage(img) {
    if (!img) return;
    var src = img.getAttribute("data-src");
    if (!src) return;
    // Dash/React dapat memakai ulang node <img> ketika user pindah group.
    // data-loaded lama tidak boleh menahan URL group baru.
    if (img.dataset.loaded === "1" && img.dataset.loadedSrc === src) return;
    img.dataset.loaded = "1";
    img.dataset.loadedSrc = src;
    img.classList.remove("image-unavailable");
    img.onerror = function () { markImageFailed(img, src); };
    img.src = src;
  }

  function setupLazyImages(root) {
    root = root || document;
    var imgs = root.querySelectorAll("img.lazy-thumb[data-src]");
    if (!imgs.length) return;

    if (!("IntersectionObserver" in window)) {
      imgs.forEach(loadImage);
      return;
    }

    if (!window.__goldenEyeLazyObserver) {
      window.__goldenEyeLazyObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting || entry.intersectionRatio > 0) {
            loadImage(entry.target);
            window.__goldenEyeLazyObserver.unobserve(entry.target);
          }
        });
      }, {
        root: null,
        rootMargin: "900px 0px",
        threshold: 0.01
      });
    }

    imgs.forEach(function (img) {
      var wanted = img.getAttribute("data-src") || "";
      if (img.dataset.loaded === "1" && img.dataset.loadedSrc !== wanted) {
        img.dataset.loaded = "0";
        img.dataset.observed = "0";
        img.dataset.failedSrc = "";
        img.classList.remove("image-unavailable");
      }
      if (img.dataset.observed === "1" || (
        img.dataset.loaded === "1" && img.dataset.loadedSrc === wanted
      )) return;
      img.dataset.observed = "1";
      window.__goldenEyeLazyObserver.observe(img);
    });
  }

  function installObserver() {
    setupLazyImages(document);
    if (window.__goldenEyeMutationObserver) return;
    window.__goldenEyeMutationObserver = new MutationObserver(function (mutations) {
      var shouldScan = false;
      for (var i = 0; i < mutations.length; i++) {
        if (
          mutations[i].type === "attributes" ||
          (mutations[i].addedNodes && mutations[i].addedNodes.length)
        ) {
          shouldScan = true;
          break;
        }
      }
      if (shouldScan) {
        window.requestAnimationFrame(function () { setupLazyImages(document); });
      }
    });
    window.__goldenEyeMutationObserver.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["data-src"]
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", installObserver);
  } else {
    installObserver();
  }
})();
