import { Outlet } from 'react-router-dom';
import { Code2, LogOut } from 'lucide-react';
import { getAuthState, logout } from '@/lib/auth';
import { useNavigate } from 'react-router-dom';

export function Layout() {
  const navigate = useNavigate();
  const auth = getAuthState();
  const roleLabel = auth.roles[0]?.replace('ROLE_', '').toLowerCase() || '';

  const handleLogout = async () => {
    await logout();
    navigate('/login', { replace: true });
  };

  return (
    <div className="flex h-screen flex-col bg-ide-bg text-gray-300">
      {/* Header */}
      <header className="flex h-9 shrink-0 items-center gap-3 border-b border-ide-border bg-ide-sidebar px-3">
        <Code2 className="h-4 w-4 text-brand-500" />
        <span className="text-sm font-semibold text-brand-400">Coding Agent IDE</span>
        <div className="ml-auto flex items-center gap-3 text-xs">
          <span className="text-green-400" title={`Tenant: ${auth.tenant}\nRoles: ${auth.roles.join(', ')}`}>
            {auth.user}
            {roleLabel && <span className="ml-1 text-gray-500">({roleLabel})</span>}
          </span>
          <button
            onClick={handleLogout}
            className="flex items-center gap-1 rounded px-2 py-1 text-gray-400 hover:bg-ide-hover hover:text-gray-200"
          >
            <LogOut className="h-3 w-3" />
            Logout
          </button>
        </div>
      </header>

      {/* Main content */}
      <main className="flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  );
}
