"""
Database operations for the Spotify Payment Bot
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
import asyncpg
from asyncpg import Pool

# Database operations - settings passed from main
from .models import (
    User, Group, Payment, PaymentStatus,
    USERS_TABLE, GROUPS_TABLE, PAYMENTS_TABLE, USER_GROUP_TABLE, INDEXES
)
from ..utils.helpers import format_display_id, format_group_name, get_now


class Database:
    """Database operations manager"""

    def __init__(self, settings):
        self.settings = settings
        self.pool: Optional[Pool] = None
        self.logger = logging.getLogger(__name__)

    async def connect(self, retries: int = 3) -> bool:
        """Connect to the database with retry logic"""

        for attempt in range(retries):
            try:
                self.logger.info(
                    f"🔄 Database connection attempt {attempt + 1}/{retries}")

                # Configure SSL for Koyeb
                import ssl
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                self.pool = await asyncpg.create_pool(
                    host=self.settings.db_host,
                    port=self.settings.db_port or 5432,
                    user=self.settings.db_username.get_secret_value(),
                    password=self.settings.db_password.get_secret_value(),
                    database=self.settings.db_database,
                    ssl=ssl_context,
                    min_size=1,
                    max_size=5,
                    command_timeout=60,
                    server_settings={
                        'application_name': 'spotify_payment_bot',
                    }
                )

                # Test connection
                async with self.pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")

                self.logger.info("✅ Database connected successfully!")
                return True

            except Exception as e:
                self.logger.error(
                    f"❌ Database connection failed (attempt {attempt + 1}): {e}")

                if "does not exist" in str(e).lower():
                    self.logger.error(
                        "💡 Database does not exist. Please create it first.")
                    break

                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff

        return False

    async def close(self):
        """Close database connection"""
        if self.pool:
            await self.pool.close()
            self.logger.info("🔌 Database connection closed")

    async def _get_next_user_display_id(self) -> str:
        """Get next available user display ID"""
        if not self.pool:
            return "001"

        try:
            async with self.pool.acquire() as conn:
                # Get highest existing display_id as integer
                max_id = await conn.fetchval(
                    "SELECT COALESCE(MAX(CAST(display_id AS INTEGER)), 0) FROM users WHERE display_id ~ '^[0-9]+$'"
                )
                next_id = (max_id or 0) + 1
                return format_display_id(next_id)
        except Exception as e:
            self.logger.error(f"Failed to get next user display ID: {e}")
            return "001"

    async def _get_next_group_display_id(self) -> str:
        """Get next available group display ID"""
        if not self.pool:
            return "001"

        try:
            async with self.pool.acquire() as conn:
                # Get highest existing display_id as integer
                max_id = await conn.fetchval(
                    "SELECT COALESCE(MAX(CAST(display_id AS INTEGER)), 0) FROM groups WHERE display_id ~ '^[0-9]+$'"
                )
                next_id = (max_id or 0) + 1
                return format_display_id(next_id)
        except Exception as e:
            self.logger.error(f"Failed to get next group display ID: {e}")
            return "001"

    async def initialize_tables(self) -> bool:
        """Initialize database tables"""
        if not self.pool:
            self.logger.error("No database connection available")
            return False

        try:
            async with self.pool.acquire() as conn:
                # Create tables
                await conn.execute(USERS_TABLE)
                await conn.execute(GROUPS_TABLE)
                await conn.execute(PAYMENTS_TABLE)
                await conn.execute(USER_GROUP_TABLE)

                # Migrate existing data to add display_ids BEFORE creating indexes
                await self._migrate_display_ids(conn)

                # Create indexes (now that display_id columns exist)
                for index_sql in INDEXES:
                    await conn.execute(index_sql)

                self.logger.info("✅ Database tables initialized successfully")
                return True

        except Exception as e:
            self.logger.error(f"❌ Failed to initialize tables: {e}")
            return False

    async def _migrate_display_ids(self, conn):
        """Migrate existing data to add display_ids where missing"""
        try:
            # Check if users table has display_id column
            users_has_display_id = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'users' AND column_name = 'display_id'
                )
            """)

            if not users_has_display_id:
                self.logger.info(
                    "🔄 Adding display_id column to users table...")
                await conn.execute("ALTER TABLE users ADD COLUMN display_id VARCHAR(3)")

                # Populate display_ids for existing users
                existing_users = await conn.fetch("SELECT user_id FROM users ORDER BY user_id")
                for i, user in enumerate(existing_users, 1):
                    display_id = format_display_id(i)
                    await conn.execute(
                        "UPDATE users SET display_id = $1 WHERE user_id = $2",
                        display_id, user['user_id']
                    )

                # Add constraints
                await conn.execute("ALTER TABLE users ADD CONSTRAINT users_display_id_unique UNIQUE (display_id)")
                await conn.execute("ALTER TABLE users ALTER COLUMN display_id SET NOT NULL")
                self.logger.info("✅ Users table migration completed")
            else:
                # Check if users need display_id migration
                users_without_display_id = await conn.fetch(
                    "SELECT user_id FROM users WHERE display_id IS NULL OR display_id = ''"
                )

                for i, user in enumerate(users_without_display_id, 1):
                    next_id = await self._get_next_user_display_id()
                    await conn.execute(
                        "UPDATE users SET display_id = $1 WHERE user_id = $2",
                        next_id, user['user_id']
                    )

            # Check if groups table has display_id column
            groups_has_display_id = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'groups' AND column_name = 'display_id'
                )
            """)

            if not groups_has_display_id:
                self.logger.info(
                    "🔄 Adding display_id column to groups table...")
                await conn.execute("ALTER TABLE groups ADD COLUMN display_id VARCHAR(3)")

                # Populate display_ids for existing groups and update names
                existing_groups = await conn.fetch("SELECT group_id, group_name FROM groups ORDER BY group_id")
                for i, group in enumerate(existing_groups, 1):
                    display_id = format_display_id(i)
                    new_group_name = format_group_name(display_id)
                    await conn.execute(
                        "UPDATE groups SET display_id = $1, group_name = $2 WHERE group_id = $3",
                        display_id, new_group_name, group['group_id']
                    )

                # Add constraints
                await conn.execute("ALTER TABLE groups ADD CONSTRAINT groups_display_id_unique UNIQUE (display_id)")
                await conn.execute("ALTER TABLE groups ALTER COLUMN display_id SET NOT NULL")
                self.logger.info("✅ Groups table migration completed")
            else:
                # Check if groups need display_id migration and group name formatting
                groups_without_display_id = await conn.fetch(
                    "SELECT group_id, group_name FROM groups WHERE display_id IS NULL OR display_id = ''"
                )

                for group in groups_without_display_id:
                    next_id = await self._get_next_group_display_id()
                    new_group_name = format_group_name(next_id)

                    await conn.execute(
                        "UPDATE groups SET display_id = $1, group_name = $2 WHERE group_id = $3",
                        next_id, new_group_name, group['group_id']
                    )

                # Also update existing groups to use spotify format if they don't already
                groups_needing_name_update = await conn.fetch(
                    "SELECT group_id, display_id FROM groups WHERE display_id IS NOT NULL AND NOT group_name LIKE 'spotify %'"
                )

                for group in groups_needing_name_update:
                    new_group_name = format_group_name(group['display_id'])
                    await conn.execute(
                        "UPDATE groups SET group_name = $1 WHERE group_id = $2",
                        new_group_name, group['group_id']
                    )

                self.logger.info("✅ Groups migration completed")

            # Migrate payment_day_of_month column
            groups_has_payment_day = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'groups' AND column_name = 'payment_day_of_month'
                )
            """)

            if not groups_has_payment_day:
                self.logger.info(
                    "🔄 Adding payment_day_of_month column to groups table...")
                await conn.execute("ALTER TABLE groups ADD COLUMN payment_day_of_month INTEGER")

                # Populate from existing next_payment_date
                existing_groups = await conn.fetch("SELECT group_id, next_payment_date FROM groups")
                for group in existing_groups:
                    payment_day = group['next_payment_date'].day
                    # Ensure it's between 1-28
                    if payment_day > 28:
                        payment_day = 28
                        self.logger.warning(
                            f"Group {group['group_id']} had payment day {group['next_payment_date'].day}, clamped to 28")

                    await conn.execute(
                        "UPDATE groups SET payment_day_of_month = $1 WHERE group_id = $2",
                        payment_day, group['group_id']
                    )

                # Add constraints
                await conn.execute("ALTER TABLE groups ADD CONSTRAINT groups_payment_day_check CHECK (payment_day_of_month BETWEEN 1 AND 28)")
                await conn.execute("ALTER TABLE groups ALTER COLUMN payment_day_of_month SET NOT NULL")
                self.logger.info("✅ payment_day_of_month migration completed")

        except Exception as e:
            self.logger.error(f"❌ Migration failed: {e}")
            raise

    # User operations
    async def add_user(self, user_id: int, username: str, first_name: Optional[str] = None) -> bool:
        """Add or update user"""
        if not self.pool:
            return False

        try:
            async with self.pool.acquire() as conn:
                # Check if user already exists
                existing = await conn.fetchrow("SELECT display_id FROM users WHERE user_id = $1", user_id)

                if existing:
                    # Update existing user
                    await conn.execute(
                        """
                        UPDATE users SET username = $2, first_name = $3
                        WHERE user_id = $1
                        """,
                        user_id, username, first_name
                    )
                else:
                    # Create new user with new display_id
                    display_id = await self._get_next_user_display_id()
                    await conn.execute(
                        """
                        INSERT INTO users (user_id, username, display_id, first_name) 
                        VALUES ($1, $2, $3, $4)
                        """,
                        user_id, username, display_id, first_name
                    )
                    self.logger.info(
                        f"Added user {username} (ID: {user_id}, Display: {display_id})")

                return True
        except Exception as e:
            self.logger.error(f"Failed to add user {user_id}: {e}")
            return False

    async def get_user(self, user_id: int) -> Optional[User]:
        """Get user by ID"""
        if not self.pool:
            return None

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT user_id, username, display_id, first_name, created_at FROM users WHERE user_id = $1",
                    user_id
                )
                return User(*row) if row else None
        except Exception as e:
            self.logger.error(f"Failed to get user {user_id}: {e}")
            return None

    # Group operations
    async def create_group(self, group_name: str, next_payment_date: datetime) -> Optional[int]:
        """Create a new payment group"""
        if not self.pool:
            return None

        try:
            async with self.pool.acquire() as conn:
                # Check if the group_name is just a number (display ID)
                if group_name.isdigit():
                    # Use the provided display ID
                    display_id = group_name
                    formatted_group_name = format_group_name(display_id)
                else:
                    # Extract display ID from group name if it's in "spotify XXX" format
                    if group_name.lower().startswith('spotify '):
                        display_id = group_name.split()[-1]
                    else:
                        # Generate next display ID for custom names
                        display_id = await self._get_next_group_display_id()
                    formatted_group_name = group_name

                # Extract payment day (ensure 1-28)
                payment_day = next_payment_date.day
                if payment_day > 28:
                    self.logger.warning(
                        f"Payment day {payment_day} exceeds 28, clamping to 28")
                    payment_day = 28
                    next_payment_date = next_payment_date.replace(day=28)

                group_id = await conn.fetchval(
                    """
                    INSERT INTO groups (group_name, display_id, payment_day_of_month, next_payment_date) 
                    VALUES ($1, $2, $3, $4) 
                    RETURNING group_id
                    """,
                    formatted_group_name, display_id, payment_day, next_payment_date
                )
                self.logger.info(
                    f"Created group '{formatted_group_name}' with ID {group_id}, Display: {display_id}, Payment day: {payment_day}")
                return group_id
        except Exception as e:
            self.logger.error(f"Failed to create group '{group_name}': {e}")
            return None

    async def get_all_groups(self) -> List[Group]:
        """Get all payment groups ordered by display_id"""
        if not self.pool:
            return []

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT group_id, group_name, display_id, payment_day_of_month, next_payment_date, created_at FROM groups ORDER BY display_id"
                )
                return [Group(*row) for row in rows]
        except Exception as e:
            self.logger.error(f"Failed to get groups: {e}")
            return []

    async def get_all_groups_for_statistics(self) -> List[Group]:
        """Get all payment groups ordered by payment day, then display_id"""
        if not self.pool:
            return []

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT group_id, group_name, display_id, payment_day_of_month, next_payment_date, created_at FROM groups ORDER BY payment_day_of_month ASC, display_id ASC"
                )
                return [Group(*row) for row in rows]
        except Exception as e:
            self.logger.error(f"Failed to get groups for statistics: {e}")
            return []

    async def get_group_by_name(self, group_name: str) -> Optional[Group]:
        """Get group by name"""
        if not self.pool:
            return None

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT group_id, group_name, display_id, payment_day_of_month, next_payment_date, created_at FROM groups WHERE group_name = $1",
                    group_name
                )
                return Group(*row) if row else None
        except Exception as e:
            self.logger.error(f"Failed to get group '{group_name}': {e}")
            return None

    async def update_group_due_date(self, group_id: int, new_due_date: datetime) -> bool:
        """Update group's next payment due date"""
        if not self.pool:
            return False

        try:
            async with self.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE groups 
                    SET next_payment_date = $1 
                    WHERE group_id = $2
                    """,
                    new_due_date.date(), group_id
                )

                # Check if any row was updated
                # Extract number from "UPDATE 1"
                rows_affected = int(result.split()[-1])

                if rows_affected > 0:
                    self.logger.info(
                        f"Updated due date for group {group_id} to {new_due_date.date()}")
                    return True
                else:
                    self.logger.warning(f"No group found with ID {group_id}")
                    return False

        except Exception as e:
            self.logger.error(
                f"Failed to update due date for group {group_id}: {e}")
            return False

    async def get_groups_with_member_count(self) -> List[dict]:
        """Get all groups with member counts for user selection"""
        if not self.pool:
            return []

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT 
                        g.group_id,
                        g.group_name,
                        g.display_id,
                        g.payment_day_of_month,
                        g.next_payment_date,
                        g.created_at,
                        COUNT(ug.user_id) as member_count
                    FROM groups g
                    LEFT JOIN user_groups ug ON g.group_id = ug.group_id
                    GROUP BY g.group_id, g.group_name, g.display_id, g.payment_day_of_month, g.next_payment_date, g.created_at
                    ORDER BY g.display_id
                """)

                return [
                    {
                        'group_id': row['group_id'],
                        'group_name': row['group_name'],
                        'display_id': row['display_id'],
                        'payment_day_of_month': row['payment_day_of_month'],
                        'next_payment_date': row['next_payment_date'],
                        'created_at': row['created_at'],
                        'member_count': row['member_count']
                    }
                    for row in rows
                ]
        except Exception as e:
            self.logger.error(f"Failed to get groups with member count: {e}")
            return []

    # User-Group associations
    async def add_user_to_group(self, user_id: int, group_id: int) -> bool:
        """Add user to a payment group and create initial 'phantom' payment to set next_payment_date"""
        if not self.pool:
            return False

        try:
            async with self.pool.acquire() as conn:
                # Add user to group
                await conn.execute(
                    """
                    INSERT INTO user_groups (user_id, group_id) 
                    VALUES ($1, $2) 
                    ON CONFLICT (user_id, group_id) DO NOTHING
                    """,
                    user_id, group_id
                )
                
                # Get group's payment day
                payment_day = await conn.fetchval(
                    "SELECT payment_day_of_month FROM groups WHERE group_id = $1",
                    group_id
                )
                
                if payment_day is None:
                    self.logger.error(f"Group {group_id} not found")
                    return False
                
                # Calculate initial next_payment_date based on join date
                # Logic: if joining within 3 days after payment_day, set to current month
                # Otherwise, set to next month
                from bot.utils.helpers import add_months_to_date
                current_time = get_now()
                current_day = current_time.day
                
                # Determine which month the first payment should be
                if current_day <= payment_day + 2:
                    # Joining within grace period (on or within 2 days after payment day)
                    # Set payment to current month
                    next_payment_date = current_time.replace(day=payment_day)
                else:
                    # Joining after grace period - set payment to next month
                    next_payment_date = add_months_to_date(current_time, 1, payment_day)
                
                # Create phantom payment with 0 months to establish next_payment_date
                # Check if phantom payment already exists
                existing_payment = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM payments 
                    WHERE user_id = $1 AND group_id = $2
                    """,
                    user_id, group_id
                )
                
                if existing_payment == 0:
                    # Create phantom payment
                    await conn.execute(
                        """
                        INSERT INTO payments (user_id, group_id, months_paid, payment_date, next_payment_date, receipt_file_id)
                        VALUES ($1, $2, 0, $3, $4, NULL)
                        """,
                        user_id, group_id, current_time, next_payment_date
                    )
                    self.logger.info(
                        f"Created phantom payment for user {user_id} in group {group_id}, next payment: {next_payment_date}")
                
                return True
        except Exception as e:
            self.logger.error(
                f"Failed to add user {user_id} to group {group_id}: {e}")
            return False

    async def get_user_group(self, user_id: int) -> Optional[Group]:
        """Get user's payment group"""
        if not self.pool:
            return None

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT g.group_id, g.group_name, g.display_id, g.payment_day_of_month, g.next_payment_date, g.created_at
                    FROM groups g
                    JOIN user_groups ug ON g.group_id = ug.group_id
                    WHERE ug.user_id = $1
                    """,
                    user_id
                )
                return Group(*row) if row else None
        except Exception as e:
            self.logger.error(f"Failed to get user group for {user_id}: {e}")
            return None

    # Payment operations
    async def add_payment(self, user_id: int, group_id: int, months_paid: int, receipt_file_id: Optional[str] = None) -> Tuple[bool, Optional[datetime]]:
        """Record a payment and return success status with next payment date
        
        Returns:
            Tuple[bool, Optional[datetime]]: (success, next_payment_date)
        """
        if not self.pool:
            return False, None

        try:
            async with self.pool.acquire() as conn:
                # Get the previous due date and payment day
                row = await conn.fetchrow(
                    """
                    SELECT 
                        COALESCE(
                            (SELECT next_payment_date FROM payments 
                             WHERE user_id = $1 AND group_id = $2 
                             ORDER BY payment_date DESC LIMIT 1),
                            (SELECT next_payment_date FROM groups WHERE group_id = $2)
                        ) as previous_due_date,
                        (SELECT payment_day_of_month FROM groups WHERE group_id = $2) as payment_day
                    """,
                    user_id, group_id
                )

                # Calculate next payment date using month arithmetic
                # This keeps payments on the same day each month
                from bot.utils.helpers import add_months_to_date
                current_payment_date = get_now()

                if row and row['previous_due_date']:
                    previous_due_date = row['previous_due_date']
                    payment_day = row['payment_day']

                    # Convert to datetime if needed
                    if not isinstance(previous_due_date, datetime):
                        previous_due_date = datetime.combine(
                            previous_due_date, datetime.min.time())

                    # Add months and set to payment day
                    next_payment_date = add_months_to_date(
                        previous_due_date, months_paid, payment_day)
                else:
                    # Fallback if no previous due date exists (shouldn't happen)
                    next_payment_date = current_payment_date + \
                        timedelta(days=30 * months_paid)

                await conn.execute(
                    """
                    INSERT INTO payments (user_id, group_id, months_paid, payment_date, next_payment_date, receipt_file_id)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    user_id, group_id, months_paid, current_payment_date, next_payment_date, receipt_file_id
                )

                self.logger.info(
                    f"Payment recorded: User {user_id}, Group {group_id}, {months_paid} months")
                return True, next_payment_date
        except Exception as e:
            self.logger.error(f"Failed to add payment for user {user_id}: {e}")
            return False, None

    async def get_user_payment_status(self, user_id: int) -> Optional[PaymentStatus]:
        """Get user's current payment status"""
        if not self.pool:
            return None

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT 
                        ug.user_id,
                        u.display_id as user_display_id,
                        g.group_id,
                        g.group_name,
                        g.display_id as group_display_id,
                        g.payment_day_of_month,
                        (SELECT next_payment_date FROM payments 
                         WHERE user_id = ug.user_id AND group_id = g.group_id 
                         ORDER BY payment_date DESC LIMIT 1) as next_payment_date,
                        (SELECT payment_date FROM payments 
                         WHERE user_id = ug.user_id AND group_id = g.group_id 
                         ORDER BY payment_date DESC LIMIT 1) as last_payment_date
                    FROM user_groups ug
                    JOIN groups g ON ug.group_id = g.group_id
                    JOIN users u ON ug.user_id = u.user_id
                    WHERE ug.user_id = $1
                    """,
                    user_id
                )

                if not row:
                    return None

                # Get next payment date from the most recent payment record
                # (which includes phantom payment set when user joined)
                next_payment = row['next_payment_date']
                
                if not next_payment:
                    # Fallback for users who joined before phantom payment implementation
                    from bot.utils.helpers import add_months_to_date
                    current_time = get_now()
                    payment_day = row['payment_day_of_month']

                    # If joining within 2 days after payment day, set to current month
                    # Otherwise, set to next month
                    if current_time.day <= payment_day + 2:
                        # Within grace period - payment due this month
                        next_payment = current_time.replace(day=payment_day)
                    else:
                        # Past grace period - payment due next month
                        next_payment = add_months_to_date(
                            current_time, 1, payment_day)

                # Convert datetime to date if needed for comparison
                if isinstance(next_payment, datetime):
                    next_payment_date = next_payment.date()
                else:
                    next_payment_date = next_payment

                # Compare dates only, not datetime (to avoid time-of-day issues)
                today = get_now().date()
                # User is overdue if payment date has arrived (including today)
                is_overdue = next_payment_date <= today

                # Calculate months remaining (rough estimate)
                days_diff = (next_payment_date - today).days
                months_remaining = max(0, days_diff // 30)

                return PaymentStatus(
                    user_id=row['user_id'],
                    user_display_id=row['user_display_id'],
                    group_id=row['group_id'],
                    group_name=row['group_name'],
                    group_display_id=row['group_display_id'],
                    next_payment_date=next_payment_date,
                    last_payment_date=row['last_payment_date'],
                    months_remaining=months_remaining,
                    is_overdue=is_overdue
                )

        except Exception as e:
            self.logger.error(
                f"Failed to get payment status for user {user_id}: {e}")
            return None

    async def get_overdue_users(self) -> List[PaymentStatus]:
        """Get all users with overdue payments"""
        if not self.pool:
            return []

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT 
                        ug.user_id,
                        u.display_id as user_display_id,
                        g.group_id,
                        g.group_name,
                        g.display_id as group_display_id,
                        g.payment_day_of_month,
                        COALESCE(
                            (SELECT next_payment_date FROM payments 
                             WHERE user_id = ug.user_id AND group_id = g.group_id 
                             ORDER BY payment_date DESC LIMIT 1),
                            g.next_payment_date
                        ) as next_payment_date,
                        (SELECT payment_date FROM payments 
                         WHERE user_id = ug.user_id AND group_id = g.group_id 
                         ORDER BY payment_date DESC LIMIT 1) as last_payment_date
                    FROM user_groups ug
                    JOIN groups g ON ug.group_id = g.group_id
                    JOIN users u ON ug.user_id = u.user_id
                    WHERE COALESCE(
                        (SELECT next_payment_date FROM payments 
                         WHERE user_id = ug.user_id AND group_id = g.group_id 
                         ORDER BY payment_date DESC LIMIT 1),
                        g.next_payment_date
                    ) < CURRENT_DATE
                    ORDER BY next_payment_date
                    """
                )

                today = get_now().date()
                result = []
                for row in rows:
                    next_payment = row['next_payment_date']
                    
                    # Convert datetime to date if needed
                    if isinstance(next_payment, datetime):
                        next_payment_date = next_payment.date()
                    else:
                        next_payment_date = next_payment
                    days_diff = (next_payment_date - today).days
                    months_remaining = max(0, days_diff // 30)

                    result.append(PaymentStatus(
                        user_id=row['user_id'],
                        user_display_id=row['user_display_id'],
                        group_id=row['group_id'],
                        group_name=row['group_name'],
                        group_display_id=row['group_display_id'],
                        next_payment_date=next_payment_date,
                        last_payment_date=row['last_payment_date'],
                        months_remaining=months_remaining,
                        is_overdue=True
                    ))

                return result

        except Exception as e:
            self.logger.error(f"Failed to get overdue users: {e}")
            return []

    async def get_users_needing_reminders(self, days_before: int = 3) -> List[PaymentStatus]:
        """Get users who need payment reminders (on due date and up to 2 days after)"""
        if not self.pool:
            return []

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT 
                        ug.user_id,
                        u.display_id as user_display_id,
                        g.group_id,
                        g.group_name,
                        g.display_id as group_display_id,
                        g.payment_day_of_month,
                        COALESCE(
                            (SELECT next_payment_date FROM payments 
                             WHERE user_id = ug.user_id AND group_id = g.group_id 
                             ORDER BY payment_date DESC LIMIT 1),
                            g.next_payment_date
                        ) as next_payment_date,
                        (SELECT payment_date FROM payments 
                         WHERE user_id = ug.user_id AND group_id = g.group_id 
                         ORDER BY payment_date DESC LIMIT 1) as last_payment_date
                    FROM user_groups ug
                    JOIN groups g ON ug.group_id = g.group_id
                    JOIN users u ON ug.user_id = u.user_id
                    WHERE DATE(COALESCE(
                        (SELECT next_payment_date FROM payments 
                         WHERE user_id = ug.user_id AND group_id = g.group_id 
                         ORDER BY payment_date DESC LIMIT 1),
                        g.next_payment_date
                    )) BETWEEN CURRENT_DATE - INTERVAL '3 days' AND CURRENT_DATE
                    ORDER BY next_payment_date
                    """
                )

                result = []
                today = get_now().date()

                for row in rows:
                    next_payment = row['next_payment_date']
                        
                    # Convert datetime to date if needed
                    if isinstance(next_payment, datetime):
                        next_payment_date = next_payment.date()
                    else:
                        next_payment_date = next_payment

                    # Calculate days overdue (negative means due in future, 0 = today, positive = overdue)
                    days_overdue = (today - next_payment_date).days

                    # Only include users who are 0-3 days overdue
                    if 0 <= days_overdue <= 3:
                        months_remaining = 0  # Already due or overdue

                        result.append(PaymentStatus(
                            user_id=row['user_id'],
                            user_display_id=row['user_display_id'],
                            group_id=row['group_id'],
                            group_name=row['group_name'],
                            group_display_id=row['group_display_id'],
                            next_payment_date=next_payment_date,
                            last_payment_date=row['last_payment_date'],
                            months_remaining=months_remaining,
                            is_overdue=(days_overdue > 0)
                        ))
                return result

        except Exception as e:
            self.logger.error(f"Failed to get users needing reminders: {e}")
            return []

    async def get_users_overdue_for_admin_warning(self, days_after: int = 3) -> List[PaymentStatus]:
        """Get users who are overdue for X days (for admin warnings)"""
        if not self.pool:
            return []

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT 
                        ug.user_id,
                        u.username,
                        u.first_name,
                        u.display_id as user_display_id,
                        g.group_id,
                        g.group_name,
                        g.display_id as group_display_id,
                        g.payment_day_of_month,
                        COALESCE(
                            (SELECT next_payment_date FROM payments 
                             WHERE user_id = ug.user_id AND group_id = g.group_id 
                             ORDER BY payment_date DESC LIMIT 1),
                            g.next_payment_date
                        ) as next_payment_date,
                        (SELECT payment_date FROM payments 
                         WHERE user_id = ug.user_id AND group_id = g.group_id 
                         ORDER BY payment_date DESC LIMIT 1) as last_payment_date
                    FROM user_groups ug
                    JOIN groups g ON ug.group_id = g.group_id
                    JOIN users u ON ug.user_id = u.user_id
                    WHERE COALESCE(
                        (SELECT next_payment_date FROM payments 
                         WHERE user_id = ug.user_id AND group_id = g.group_id 
                         ORDER BY payment_date DESC LIMIT 1),
                        g.next_payment_date
                    ) <= CURRENT_DATE - $1 * INTERVAL '1 day'
                    ORDER BY next_payment_date
                    """,
                    days_after
                )

                result = []
                for row in rows:
                    next_payment = row['next_payment_date']
                        
                    if isinstance(next_payment, datetime):
                        next_payment = next_payment.date()
                    days_diff = (get_now().date() - next_payment).days

                    # Create PaymentStatus with user info for admin warnings
                    status = PaymentStatus(
                        user_id=row['user_id'],
                        user_display_id=row['user_display_id'],
                        group_id=row['group_id'],
                        group_name=row['group_name'],
                        group_display_id=row['group_display_id'],
                        next_payment_date=next_payment,
                        last_payment_date=row['last_payment_date'],
                        months_remaining=0,
                        is_overdue=True,
                        username=row['username'],
                        first_name=row['first_name'],
                        days_overdue=days_diff
                    )

                    result.append(status)

                return result

        except Exception as e:
            self.logger.error(
                f"Failed to get users overdue for admin warning: {e}")
            return []
            return []

    async def is_user_registered(self, user_id: int) -> bool:
        """Check if user is registered in any group"""
        if not self.pool:
            return False

        try:
            async with self.pool.acquire() as conn:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM user_groups WHERE user_id = $1",
                    user_id
                )
                return count > 0
        except Exception as e:
            self.logger.error(
                f"Failed to check user registration for {user_id}: {e}")
            return False

    async def get_group_by_display_id(self, display_id: str) -> Optional[Group]:
        """Get group by display ID (as string)"""
        if not self.pool:
            return None

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT group_id, group_name, display_id, payment_day_of_month, next_payment_date, created_at
                    FROM groups 
                    WHERE display_id = $1
                    """,
                    str(display_id)
                )

                return Group(*row) if row else None

        except Exception as e:
            self.logger.error(
                f"Failed to get group by display_id {display_id}: {e}")
            return None

    async def get_group_by_name_or_id(self, identifier: str) -> Optional[Group]:
        """Get group by either full name or display ID (e.g., 'spotify 001' or '001')"""
        if not self.pool:
            return None

        try:
            # First try as display ID (if it's a number)
            if identifier.isdigit():
                group = await self.get_group_by_display_id(identifier)
                if group:
                    return group

            # Try as full group name
            group = await self.get_group_by_name(identifier)
            return group

        except Exception as e:
            self.logger.error(
                f"Failed to get group by identifier '{identifier}': {e}")
            return None

    async def bulk_import_groups(self, groups_data: List[dict]) -> int:
        """Import multiple groups at once"""
        if not self.pool:
            return 0

        success_count = 0

        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    for group_data in groups_data:
                        try:
                            # Get next payment date (30 days from now)
                            next_payment_date = get_now() + timedelta(days=30)

                            # Insert group
                            group_id = await conn.fetchval(
                                """
                                INSERT INTO groups (group_name, next_payment_date, display_id) 
                                VALUES ($1, $2, $3)
                                RETURNING group_id
                                """,
                                group_data['name'],
                                next_payment_date,
                                group_data['display_id']
                            )

                            if group_id:
                                success_count += 1
                                self.logger.info(
                                    f"Created group: {group_data['name']} (ID: {group_data['display_id']})")

                        except Exception as e:
                            self.logger.error(
                                f"Failed to create group {group_data['name']}: {e}")
                            continue

                    self.logger.info(
                        f"Bulk import completed: {success_count}/{len(groups_data)} groups created")
                    return success_count

        except Exception as e:
            self.logger.error(f"Failed to bulk import groups: {e}")
            return 0

    async def delete_group(self, group_id: int) -> bool:
        """Delete a group and all associated data"""
        if not self.pool:
            return False

        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # First, get group info for logging
                    group = await conn.fetchrow(
                        "SELECT group_name, display_id FROM groups WHERE group_id = $1",
                        group_id
                    )

                    if not group:
                        self.logger.warning(
                            f"Attempted to delete non-existent group {group_id}")
                        return False

                    # Delete payments first (foreign key constraint)
                    await conn.execute(
                        "DELETE FROM payments WHERE group_id = $1",
                        group_id
                    )

                    # Delete user-group associations
                    await conn.execute(
                        "DELETE FROM user_groups WHERE group_id = $1",
                        group_id
                    )

                    # Finally delete the group
                    await conn.execute(
                        "DELETE FROM groups WHERE group_id = $1",
                        group_id
                    )

                    self.logger.info(
                        f"Deleted group: {group['group_name']} (ID: {group['display_id']})")
                    return True

        except Exception as e:
            self.logger.error(f"Failed to delete group {group_id}: {e}")
            return False

    async def get_group_members(self, group_id: int) -> List[dict]:
        """Get all members of a group with their details"""
        if not self.pool:
            return []

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT u.user_id, u.username, u.first_name, u.display_id,
                           COUNT(p.payment_id) as total_payments,
                           MAX(p.payment_date) as last_payment,
                           g.payment_day_of_month,
                           (SELECT next_payment_date FROM payments 
                            WHERE user_id = u.user_id AND group_id = $1 
                            ORDER BY payment_date DESC LIMIT 1) as next_payment_date
                    FROM users u
                    JOIN user_groups ug ON u.user_id = ug.user_id
                    JOIN groups g ON ug.group_id = g.group_id
                    LEFT JOIN payments p ON u.user_id = p.user_id AND p.group_id = $1
                    WHERE ug.group_id = $1
                    GROUP BY u.user_id, u.username, u.first_name, u.display_id, g.payment_day_of_month
                    ORDER BY u.display_id
                    """,
                    group_id
                )

                from bot.utils.helpers import add_months_to_date
                today = get_now().date()
                current_time = get_now()
                members = []
                for row in rows:
                    next_payment = row['next_payment_date']
                    
                    if not next_payment:
                        # Fallback for users who joined before phantom payment implementation
                        payment_day = row['payment_day_of_month']

                        # If joining within 2 days after payment day, set to current month
                        # Otherwise, set to next month
                        if current_time.day <= payment_day + 2:
                            # Within grace period - payment due this month
                            next_payment = current_time.replace(day=payment_day)
                        else:
                            # Past grace period - payment due next month
                            next_payment = add_months_to_date(
                                current_time, 1, payment_day)
                    
                    if next_payment:
                        # Convert datetime to date for comparison
                        if isinstance(next_payment, datetime):
                            next_payment_date = next_payment.date()
                        else:
                            next_payment_date = next_payment

                        # User is overdue if payment date has arrived (including today)
                        is_overdue = next_payment_date <= today

                    members.append({
                        'user_id': row['user_id'],
                        'username': row['username'],
                        'first_name': row['first_name'],
                        'display_id': row['display_id'],
                        'total_payments': row['total_payments'] or 0,
                        'last_payment': row['last_payment'],
                        'next_payment_date': next_payment_date,
                        'is_overdue': is_overdue
                    })

                return members

        except Exception as e:
            self.logger.error(
                f"Failed to get group members for group {group_id}: {e}")
            return []

    async def remove_user_from_group(self, user_id: int, group_id: int) -> bool:
        """Remove a user from a group"""
        if not self.pool:
            return False

        try:
            async with self.pool.acquire() as conn:
                # Check if user is in the group
                exists = await conn.fetchval(
                    "SELECT COUNT(*) FROM user_groups WHERE user_id = $1 AND group_id = $2",
                    user_id, group_id
                )

                if not exists:
                    return False

                # Remove user from group
                await conn.execute(
                    "DELETE FROM user_groups WHERE user_id = $1 AND group_id = $2",
                    user_id, group_id
                )

                self.logger.info(
                    f"Removed user {user_id} from group {group_id}")
                return True

        except Exception as e:
            self.logger.error(
                f"Failed to remove user {user_id} from group {group_id}: {e}")
            return False

    async def get_user_payment_history(self, user_id: int) -> List[Payment]:
        """Get payment history for a user"""
        if not self.pool:
            return []

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT payment_id, user_id, group_id, months_paid, 
                           payment_date, next_payment_date, receipt_file_id
                    FROM payments 
                    WHERE user_id = $1 
                    ORDER BY payment_date DESC
                    """,
                    user_id
                )

                payments = []
                for row in rows:
                    payments.append(Payment(
                        payment_id=row['payment_id'],
                        user_id=row['user_id'],
                        group_id=row['group_id'],
                        months_paid=row['months_paid'],
                        payment_date=row['payment_date'],
                        next_payment_date=row['next_payment_date'],
                        receipt_file_id=row['receipt_file_id']
                    ))

                return payments

        except Exception as e:
            self.logger.error(
                f"Failed to get payment history for user {user_id}: {e}")
            return []
