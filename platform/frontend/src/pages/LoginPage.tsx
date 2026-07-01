import { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { Code2 } from 'lucide-react';
import { login } from '@/lib/auth';

export function LoginPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await login(username, password);
      navigate('/', { replace: true });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Login failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex h-screen items-center justify-center bg-ide-bg">
      <div className="w-[360px] rounded-lg border border-ide-border bg-ide-sidebar p-8">
        <div className="mb-6 flex items-center gap-2">
          <Code2 className="h-6 w-6 text-brand-500" />
          <h2 className="text-lg font-semibold text-gray-100">Coding Agent Login</h2>
        </div>

        <form onSubmit={handleSubmit} className="space-y-3">
          <input
            type="text"
            placeholder="Username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="w-full rounded border border-ide-border bg-ide-panel px-3 py-2 text-sm text-gray-200 outline-none focus:border-brand-500"
            autoFocus
          />
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded border border-ide-border bg-ide-panel px-3 py-2 text-sm text-gray-200 outline-none focus:border-brand-500"
          />
          {error && <p className="text-xs text-red-400">{error}</p>}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded bg-brand-600 py-2 text-sm font-medium text-white hover:bg-brand-500 disabled:bg-ide-panel disabled:cursor-not-allowed"
          >
            {loading ? 'Logging in...' : 'Login'}
          </button>
        </form>

        <p className="mt-4 text-center text-xs text-gray-500">
          Don't have an account?{' '}
          <Link to="/signup" className="text-brand-400 hover:underline">
            Sign up
          </Link>
        </p>
      </div>
    </div>
  );
}
