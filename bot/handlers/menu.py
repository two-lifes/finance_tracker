from aiogram import Router

# Entry points (menu buttons and commands) register here. This router is included before
# all dialog routers so a menu button interrupts any dialog instead of being read as input.
menu_router = Router(name="menu")
