/* Receipt-only paper settings. No transaction data or calculations change here. */
(() => {
  const autoPrint = document.currentScript.dataset.autoPrint === "true";
  const ticket = document.getElementById("printable-ticket");
  const inputs = [...document.querySelectorAll('input[name="ticket_width"]')];
  const formats = ["58", "80", "letter", "a4"];
  const storageKey = "patia.ticket.print-format.v1";
  const pageStyle = document.createElement("style");
  pageStyle.id = "ticket-paper-size";
  document.head.append(pageStyle);
  let selected = "80";
  try {
    const saved = localStorage.getItem(storageKey);
    if (formats.includes(saved)) selected = saved;
  } catch (_) { /* Printing must work when browser storage is unavailable. */ }

  function preparePaper() {
    if (selected === "letter" || selected === "a4") {
      pageStyle.textContent = `@page { size: ${selected === "letter" ? "Letter" : "A4"} portrait; margin: 12mm; }`;
      return;
    }
    // "58mm auto" is not a valid CSS page size. Measure a natural-height roll
    // in the print layout (also when printing via the browser's own menu).
    ticket.classList.add("ticket-measuring");
    const height = Math.ceil(ticket.getBoundingClientRect().height * 25.4 / 96) + 2;
    ticket.classList.remove("ticket-measuring");
    // Stay below the common PDF 200-inch page limit. Exceptional receipts
    // continue on another roll-sized page rather than losing their last lines.
    pageStyle.textContent = `@page { size: ${selected}mm ${Math.min(Math.max(height, 40), 5000)}mm; margin: 0; }`;
  }

  function applyFormat(value, persist = false) {
    selected = formats.includes(value) ? value : "80";
    inputs.forEach(input => { input.checked = input.value === selected; });
    ticket.dataset.printFormat = selected;
    ticket.classList.toggle("ticket-print-v2--58", selected === "58");
    ticket.classList.toggle("ticket-print-v2--80", selected === "80");
    document.documentElement.dataset.ticketWidth = selected;
    if (persist) {
      try { localStorage.setItem(storageKey, selected); } catch (_) { /* Optional preference. */ }
    }
    preparePaper();
  }

  async function printTicket() {
    if (document.fonts) await document.fonts.ready;
    preparePaper();
    window.print();
  }

  inputs.forEach(input => input.addEventListener("change", () => applyFormat(input.value, true)));
  document.getElementById("print-ticket").addEventListener("click", printTicket);
  document.getElementById("save-ticket-pdf").addEventListener("click", () => {
    document.getElementById("pdf-help").focus({preventScroll: true});
    printTicket();
  });
  window.addEventListener("beforeprint", preparePaper);
  window.addEventListener("resize", preparePaper);
  applyFormat(selected);
  if (document.fonts) document.fonts.ready.then(preparePaper);
  if (autoPrint) window.addEventListener("load", printTicket, {once: true});
})();
