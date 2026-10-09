(() => {
  const button = document.querySelector(".auth-password__toggle");
  const input = document.getElementById("password");
  if (!button || !input) return;
  button.addEventListener("click", () => {
    const visible = input.type === "password";
    input.type = visible ? "text" : "password";
    button.setAttribute("aria-pressed", String(visible));
    button.setAttribute("aria-label", visible ? button.dataset.hideLabel : button.dataset.showLabel);
    button.querySelector(".auth-password__slash").toggleAttribute("hidden", !visible);
  });
})();
