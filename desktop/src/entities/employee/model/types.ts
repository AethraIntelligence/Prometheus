export interface Employee {
  id: string;
  name: string;
  title: string;
  description: string;
  tools: string[];
  /** Services granted in the employee's own file. */
  integrations?: string[];
  limits: {
    max_steps: number;
    max_cost_usd: number;
    max_wall_time_seconds: number;
  };
}
