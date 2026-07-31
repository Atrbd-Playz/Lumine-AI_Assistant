import {
    Mic,
    Settings,
    Brain,
    Plug,
} from "lucide-react";

export default function Home() {
    return (
        <main className="min-h-screen bg-slate-950 text-white flex flex-col">

            {/* Top Bar */}

            <header className="h-16 border-b border-slate-800 flex items-center justify-between px-6">

                <h1 className="text-xl font-semibold tracking-wide">
                    ✦ Lumine
                </h1>

                <button className="p-2 rounded-lg hover:bg-slate-800">
                    <Settings size={20}/>
                </button>

            </header>


            {/* Center */}

            <section className="flex-1 flex flex-col justify-center items-center">

                {/* Avatar */}

                <div className="w-56 h-56 rounded-full border border-cyan-500/20 bg-slate-900 flex items-center justify-center shadow-2xl">

                    <div className="space-y-6">

                        <div className="flex gap-8 justify-center">

                            <div className="w-5 h-5 rounded bg-cyan-400"/>

                            <div className="w-5 h-5 rounded bg-cyan-400"/>

                        </div>

                        <div className="w-24 h-2 rounded-full bg-cyan-400 mx-auto"/>

                    </div>

                </div>

                <h2 className="mt-8 text-3xl font-bold">
                    Lumine
                </h2>

                <p className="mt-2 text-slate-400">
                    Ready to chat
                </p>

               

                <button className="mt-10 flex items-center gap-3 bg-cyan-500 hover:bg-cyan-400 transition px-8 py-4 rounded-full text-black font-semibold">

                    <Mic size={20}/>

                    Start Conversation

                </button>

            </section>



            {/* Bottom */}

            <footer className="border-t border-slate-800 h-16 flex justify-center gap-10 items-center">

                <button className="flex items-center gap-2 text-slate-400 hover:text-cyan-400">

                    <Brain size={18}/>

                    Memory

                </button>

                <button className="flex items-center gap-2 text-slate-400 hover:text-cyan-400">

                    <Settings size={18}/>

                    Settings

                </button>

                <button className="flex items-center gap-2 text-slate-400 hover:text-cyan-400">

                    <Plug size={18}/>

                    Plugins

                </button>

            </footer>

        </main>
    );
}