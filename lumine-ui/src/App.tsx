import { Toaster } from "@/components/ui/toast";
import { HintProvider } from "@/components/ui/hint";
import Home from "./pages/Home";

/**
 * The app root.
 *
 * The two providers here exist for timing rather than for state. `HintProvider`
 * shares one hover delay across every tooltip, so a row of them does not strobe as
 * the pointer passes over and the second one opens instantly once the first has
 * shown — which is how people actually read a group of hints. `Toaster` owns the
 * notification queue, which is global by nature: a tool call reported from the
 * voice session and one raised by a settings screen have to be able to queue
 * behind each other rather than each keeping its own.
 */
function App() {
  return (
    <HintProvider>
      <Home />
      <Toaster />
    </HintProvider>
  );
}

export default App;
