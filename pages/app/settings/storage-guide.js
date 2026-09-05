// Keep tutorial navigation separate from editable provider settings.
const mask = document.getElementById("storage-guide-mask");
const panels = [...mask.querySelectorAll("[data-storage-guide]")];

export function openStorageGuide(provider) {
  const current = panels.find(
    (panel) => panel.dataset.storageGuide === provider,
  );
  if (!current) return;
  for (const panel of panels) panel.hidden = panel !== current;
  document.getElementById("storage-guide-title").textContent = `${
    current.querySelector("h4").textContent
  } 配置教程`;
  mask.classList.remove("hidden");
  mask.setAttribute("aria-hidden", "false");
  mask.querySelector(".storage-guide-body").scrollTop = 0;
}

const close = document.getElementById("storage-guide-close");
close.addEventListener("click", () => {
  mask.classList.add("hidden");
  mask.setAttribute("aria-hidden", "true");
});
mask.addEventListener("click", (event) => {
  if (event.target === mask) close.click();
});
