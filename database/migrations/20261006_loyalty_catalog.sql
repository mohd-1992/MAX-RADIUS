-- Preserve existing rewards and enable financial rewards.
CREATE TABLE IF NOT EXISTS wisp_loyalty_wallets (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(64) NOT NULL UNIQUE,
                    points_balance INT DEFAULT 0,
                    total_points_earned INT DEFAULT 0,
                    total_points_redeemed INT DEFAULT 0,
                    tier_level ENUM('bronze', 'silver', 'gold', 'vip') DEFAULT 'bronze',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_points_user (username)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS wisp_loyalty_rewards (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    reward_name VARCHAR(120) NOT NULL,
                    points_cost INT NOT NULL,
                    reward_type VARCHAR(50) DEFAULT 'data_bonus_mb',
                    reward_value BIGINT NOT NULL,
                    description VARCHAR(255) NULL,
                    icon VARCHAR(50) DEFAULT 'fa-gift',
                    is_active TINYINT(1) DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS wisp_loyalty_transactions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(64) NOT NULL,
                    transaction_type VARCHAR(50) NOT NULL,
                    points INT NOT NULL,
                    balance_after INT NOT NULL,
                    notes VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_trx_user (username)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
ALTER TABLE wisp_loyalty_rewards ADD COLUMN IF NOT EXISTS icon VARCHAR(50) DEFAULT 'fa-gift';
ALTER TABLE wisp_loyalty_rewards MODIFY COLUMN reward_type VARCHAR(50) DEFAULT 'data_bonus_mb';
