import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

// A deliberately ordinary React application: controlled inputs, a keyed list,
// an async effect that hydrates late, and a portal. These are the four shapes
// a browser agent most often gets wrong on a real React page -- a controlled
// input whose value is reset by a re-render, a list whose DOM nodes are reused
// when it is reversed, content that does not exist on first paint, and a modal
// rendered outside its parent in the document.
function App() {
  const [name, setName] = useState("");
  const [savedName, setSavedName] = useState("(not saved)");
  const [items, setItems] = useState(["Alpha", "Bravo", "Charlie"]);
  const [hydrated, setHydrated] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const nextId = useRef(4);

  useEffect(() => {
    const timer = window.setTimeout(() => setHydrated(true), 180);
    return () => window.clearTimeout(timer);
  }, []);

  return React.createElement(
    "main",
    null,
    React.createElement("h1", null, "React Certification Fixture"),
    React.createElement(
      "p",
      { id: "hydration-state" },
      hydrated ? "hydrated" : "hydrating",
    ),
    React.createElement(
      "section",
      { id: "controlled" },
      React.createElement("h2", null, "Controlled Input"),
      React.createElement(
        "label",
        { htmlFor: "name-input" },
        "Your Name",
      ),
      React.createElement("input", {
        id: "name-input",
        value: name,
        placeholder: "Type here",
        onChange: (event) => setName(event.target.value),
      }),
      React.createElement(
        "button",
        {
          id: "save-name",
          onClick: () => setSavedName(name || "(empty)"),
        },
        "Save Name",
      ),
      React.createElement("output", { id: "saved-name" }, savedName),
    ),
    React.createElement(
      "section",
      { id: "list" },
      React.createElement("h2", null, "Keyed List"),
      React.createElement(
        "ul",
        null,
        items.map((item, position) =>
          React.createElement(
            "li",
            { key: item, "data-item": item },
            `${position + 1}. ${item}`,
            " ",
            React.createElement(
              "button",
              {
                "aria-label": `Remove ${item}`,
                onClick: () =>
                  setItems((current) => current.filter((value) => value !== item)),
              },
              "Remove",
            ),
          ),
        ),
      ),
      React.createElement(
        "button",
        {
          id: "reverse-list",
          onClick: () => setItems((current) => [...current].reverse()),
        },
        "Reverse List",
      ),
      React.createElement(
        "button",
        {
          id: "add-item",
          onClick: () => {
            const id = nextId.current++;
            setItems((current) => [...current, `Item ${id}`]);
          },
        },
        "Add Item",
      ),
    ),
    React.createElement(
      "section",
      { id: "portal" },
      React.createElement("h2", null, "Portal Dialog"),
      React.createElement(
        "button",
        { id: "open-modal", onClick: () => setModalOpen(true) },
        "Open React Modal",
      ),
      modalOpen
        ? React.createElement(
            "div",
            {
              role: "dialog",
              "aria-modal": "true",
              "aria-label": "React portal dialog",
              id: "react-dialog",
            },
            React.createElement("p", null, "This dialog is rendered in a portal."),
            React.createElement(
              "button",
              { id: "close-modal", onClick: () => setModalOpen(false) },
              "Close Dialog",
            ),
          )
        : null,
    ),
    React.createElement(
      "div",
      { id: "react-version", "data-testid": "react-version" },
      `React ${React.version}`,
    ),
  );
}

const root = createRoot(document.getElementById("root"));
root.render(React.createElement(App));
