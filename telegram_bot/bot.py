import logging
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from utils.logger import get_logger

logger = get_logger('telegram_bot')

class ArbitrageBot:
    """
    Telegram bot for controlling the arbitrage engine.
    Only responds to the user specified in TELEGRAM_USER_ID.
    """
    
    def __init__(self, token: str, user_id: int, engine=None):
        self.token = token
        self.user_id = int(user_id)
        self.engine = engine  # Reference to main engine
        self.app: Application = None
        self.notifier = None
    
    async def setup(self):
        """Initialize bot application."""
        self.app = Application.builder().token(self.token).build()
        
        # Register commands
        from telegram_bot.commands import CommandHandler as CmdHandler
        cmds = CmdHandler(self.engine, self.user_id)
        
        self.app.add_handler(CommandHandler('start', cmds.cmd_start))
        self.app.add_handler(CommandHandler('help', cmds.cmd_help))
        self.app.add_handler(CommandHandler('auto', cmds.cmd_auto))
        self.app.add_handler(CommandHandler('status', cmds.cmd_status))
        self.app.add_handler(CommandHandler('mode', cmds.cmd_mode))
        self.app.add_handler(CommandHandler('portfolio', cmds.cmd_portfolio))
        self.app.add_handler(CommandHandler('history', cmds.cmd_history))
        self.app.add_handler(CommandHandler('top', cmds.cmd_top))
        self.app.add_handler(CommandHandler('cancel', cmds.cmd_cancel))
        
        # Set bot commands menu (tolerate timeout)
        commands = [
            BotCommand('start', 'Start bot introduction'),
            BotCommand('help', 'List all commands'),
            BotCommand('auto', 'Start/stop automation (on/off)'),
            BotCommand('status', 'Engine status & connections'),
            BotCommand('mode', 'Switch paper/live mode'),
            BotCommand('portfolio', 'Account balances & positions'),
            BotCommand('history', 'Trade history & PnL'),
            BotCommand('top', 'Top spread pairs'),
            BotCommand('cancel', 'Cancel pending orders'),
        ]
        try:
            await self.app.bot.set_my_commands(commands)
            logger.info("Bot commands registered")
        except Exception as e:
            logger.warning("Could not set bot commands (will retry): %s", e)
    
    async def start(self):
        """Start polling."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info(f"Bot started. Authorized user: {self.user_id}")
    
    async def stop(self):
        """Stop bot gracefully."""
        if self.app:
            try:
                if self.app.updater and self.app.updater.running:
                    await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception as e:
                logger.warning("Bot stop error (non-fatal): %s", e)
            logger.info("Bot stopped")
