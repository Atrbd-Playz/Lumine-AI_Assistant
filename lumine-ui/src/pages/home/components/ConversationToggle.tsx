import { Icon } from "./Icon";

export function ConversationToggle({ open, onClick }: { open: boolean; onClick: () => void }) {
  return <button
    className={`conversation-toggle ${open ? "is-open" : ""}`}
    onClick={onClick}
    aria-expanded={open}
    aria-controls="conversation-panel"
    aria-label={open ? "Close conversation activity" : "Open conversation activity"}
    title={open ? "Close conversation activity" : "Open conversation activity"}
  >
    <Icon name="activity" size={17} />
  </button>;
}
