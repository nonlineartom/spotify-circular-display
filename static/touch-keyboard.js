/* Shared touch input for the keyboardless kiosk. No search text is stored. */
(function (root) {
  "use strict";

  function editTouchText(value, start, end, key, maxLength = 120) {
    value = String(value);
    start = Math.max(0, Math.min(value.length, start == null ? value.length : start));
    end = Math.max(start, Math.min(value.length, end == null ? start : end));
    if (key === "Clear") return {value:"", caret:0};
    if (key === "Backspace") {
      if (start === end && start > 0) start -= [...value.slice(0, start)].pop().length;
      return {value:value.slice(0, start) + value.slice(end), caret:start};
    }
    const available = Math.max(0, maxLength - (value.length - (end - start)));
    let insert = "";
    for (const character of String(key)) {
      if (insert.length + character.length > available) break;
      insert += character;
    }
    return {value:value.slice(0, start) + insert + value.slice(end), caret:start + insert.length};
  }

  if (typeof module !== "undefined" && module.exports) module.exports = {editTouchText};
  if (!root || !root.document) return;

  root.createTouchKeyboard = function createTouchKeyboard({element, onDone, onChange}) {
    let input = null, symbols = false, shifted = false, doneLabel = "Done";
    element.classList.add("touch-keyboard");
    element.setAttribute("role", "group");
    element.setAttribute("aria-label", "On-screen keyboard");
    // Retain the field's caret/selection when a finger presses a key.
    element.addEventListener("pointerdown", event => {
      if (event.target.closest("button")) event.preventDefault();
    });

    function edit(key) {
      if (!input) return;
      const limit = input.maxLength >= 0 ? input.maxLength : 120;
      const next = editTouchText(input.value, input.selectionStart, input.selectionEnd, key, limit);
      input.value = next.value;
      input.focus({preventScroll:true});
      if (["text", "search", "password", "tel", "url"].includes(input.type)) input.setSelectionRange(next.caret, next.caret);
      input.dispatchEvent(new Event("input", {bubbles:true}));
      if (onChange) onChange(input);
    }

    function keyButton(label, action, className = "", accessibleLabel = label) {
      const key = document.createElement("button");
      key.type = "button"; key.textContent = label; key.className = "touch-key " + className;
      key.setAttribute("aria-label", accessibleLabel);
      key.addEventListener("click", action);
      return key;
    }

    function render() {
      const rows = symbols
        ? [[..."1234567890"], ["-", "/", ":", "_", "(", ")", "&", "@", '"'], [".", ",", "?", "!", "'", "+", "#"]]
        : [[..."qwertyuiop"], [..."asdfghjkl"], [..."zxcvbnm"]];
      element.replaceChildren(...rows.map((letters, index) => {
        const row = document.createElement("div"); row.className = "touch-key-row";
        if (index === 2 && !symbols) {
          const shift = keyButton("⇧", () => {shifted = !shifted; render();}, "touch-key-shift", "Shift");
          shift.setAttribute("aria-pressed", String(shifted)); row.appendChild(shift);
        }
        letters.forEach(letter => row.appendChild(keyButton(letter.toUpperCase(), () => {
          edit(shifted && !symbols ? letter.toUpperCase() : letter);
          if (shifted && !symbols) {shifted = false; render();}
        })));
        if (index === 2) row.appendChild(keyButton("⌫", () => edit("Backspace"), "touch-key-backspace", "Backspace"));
        return row;
      }));
      const actions = document.createElement("div"); actions.className = "touch-key-row touch-key-actions";
      actions.append(
        keyButton(symbols ? "ABC" : "123", () => {symbols = !symbols; render();}, "touch-key-mode", symbols ? "Letters" : "Numbers and symbols"),
        keyButton("Space", () => edit(" "), "touch-key-space"),
        keyButton("Clear", () => edit("Clear")),
        keyButton(doneLabel, () => {if(input) input.dispatchEvent(new Event("change", {bubbles:true})); if(onDone) onDone();}, "touch-key-done")
      );
      element.appendChild(actions);
    }

    return {
      open(field, options = {}) {
        input = field; symbols = !!options.numeric; shifted = false; doneLabel = options.doneLabel || "Done";
        element.hidden = false; render(); input.focus({preventScroll:true});
      },
      close() {input = null; element.hidden = true;},
      isOpen() {return input !== null;},
    };
  };
})(typeof window !== "undefined" ? window : null);
